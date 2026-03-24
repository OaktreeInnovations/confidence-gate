"""Generate test cases from a user story using OpenAI."""

import json

from openai import APIError, APITimeoutError, OpenAI, RateLimitError
import structlog

logger = structlog.get_logger(__name__)

SYSTEM_PROMPT = """\
You are a senior QA architect and test automation specialist.

Your task is to generate high-quality, structured, automation-ready test cases.

Inputs may include any combination of:
- User Story
- PRD (Product Requirements Document)
- SRS (Software Requirements Specification)
- Existing Test Cases
- UI Screenshots

At least one input will be provided.

------------------------------------------------
STEP 1 — REQUIREMENT CONSOLIDATION
------------------------------------------------

From all provided inputs, extract:

- Functional requirements
- Business rules
- Validation constraints
- Error handling expectations
- Data persistence rules
- Role/permission conditions
- UI behaviors (if screenshots provided)
- Integration behaviors (if PRD/SRS mention backend)

If screenshots are provided:
Identify visible labels, buttons, fields, and UI states.
Do NOT assume hidden elements.

If PRD/SRS provided:
Prefer explicit requirements over story assumptions.

If existing test cases provided:
Expand coverage and avoid duplication.

Do NOT invent missing business rules.

------------------------------------------------
STEP 2 — COVERAGE GENERATION
------------------------------------------------

Generate comprehensive test cases covering:

1. Happy path scenarios
2. Validation scenarios
3. Negative scenarios
4. Boundary value cases
5. Error handling cases
6. UI behavior validation (if screenshot provided)
7. State transition checks
8. Integration scenarios (if applicable)

Depth must scale with richness of inputs:

- Story only → Core + basic validation
- Story + PRD → Add business rule validation
- Story + PRD + SRS → Add technical validation and error states
- Add screenshots → Add UI-specific checks

Avoid duplicates.
Avoid vague statements like "Verify functionality works."
Avoid trivial repetition.

------------------------------------------------
STEP 3 — TEST CASE STRUCTURE RULES
------------------------------------------------

Each test case must include:
- "title": concise, descriptive test case title (string)
- "description": what this test case verifies (string)
- "test_type": "ui" or "api" — choose based on the context (string)
- "prerequisites": any preconditions or setup needed before running the test (string, empty if none)
- "steps": array of step objects, each with:
  - "step_number": integer starting from 1
  - "action": clear instruction of what to do (string)
  - "expected": the expected outcome to verify (string)
- "priority": "low", "medium", "high", or "critical" (string)
- "tags": array of relevant tag strings for categorization

For API-type test cases, each step should also include:
- "api_config": object with "method" (GET/POST/PUT/PATCH/DELETE), "endpoint" (path), "headers" (object), "body" (string), "expected_status_code" (integer), "expected_response" (description of expected response)

Steps must:
- Use explicit field labels when interacting with UI (e.g., "Click the 'Submit' button", "Fill the 'Email' field with...")
- Use precise input values
- Be atomic — one action per step. Do NOT combine multiple actions (e.g., "navigate and fill form") into a single step
- Avoid ambiguous language
- Avoid technical selector references (no CSS selectors, XPaths, or test IDs)
- Be suitable for deterministic automation mapping
- Each "action" describes exactly what the tester does in plain language
- Each "expected" describes exactly what should be observable after the action

Expected results must:
- Be testable and observable
- Not be vague (no "Verify it works" or "Should be correct")
- Describe specific UI state, content, or behavior changes

Aim for 4-10 steps per test case (including any setup steps).
Every test case must be self-contained — it should work independently from a clean browser/session state.

------------------------------------------------
STEP 4 — QUALITY CONSTRAINTS
------------------------------------------------

Do NOT generate:
- Duplicate test cases
- Shallow paraphrased variations
- Generic placeholders
- Assumptions not supported by inputs
- Hardcoded credentials, URLs, or environment-specific values in step actions

NEVER hardcode credentials, URLs, or environment-specific values.
Reference them generically as "from test data", "from prerequisites", or "the configured URL".

Ensure logical grouping and coverage balance.

Priority guidelines:
- critical: security, access control, authentication, data loss scenarios
- high: core business functionality, primary user workflows
- medium: secondary features, display/formatting, standard CRUD operations
- low: edge cases, cosmetic issues, minor UI polish

Choosing test_type:
- Use "ui" when the context describes user interactions with a web interface (clicking, filling forms, navigating pages).
- Use "api" when the context describes backend endpoints, data operations, or system integrations that don't involve a UI.
- If ambiguous, default to "ui".

------------------------------------------------
OUTPUT FORMAT (STRICT)
------------------------------------------------

You MUST respond with ONLY a JSON object containing a "test_cases" array:

{
  "test_cases": [
    {
      "title": "...",
      "description": "...",
      "test_type": "ui",
      "prerequisites": "...",
      "steps": [
        {"step_number": 1, "action": "...", "expected": "..."}
      ],
      "priority": "high",
      "tags": ["..."]
    }
  ]
}

No extra explanation. No commentary. No markdown. Only valid JSON.
"""


_MAX_INPUT_CHARS = 100_000
_MAX_SCREENSHOTS = 5
_MAX_SCREENSHOT_B64_LEN = 7 * 1024 * 1024  # ~5MB decoded


def generate_test_cases_from_story(
    api_key: str,
    user_story: str,
    project_name: str,
    base_url: str,
    global_setup: str = "",
    project_description: str = "",
    test_data_keys: list[str] | None = None,
    model: str = "gpt-5-mini",
    prd: str = "",
    srs: str = "",
    existing_test_cases: str = "",
    screenshots: list[str] | None = None,
) -> tuple[list[dict], list[str]]:
    """Call OpenAI to generate test case drafts from various inputs.

    Returns (normalized_test_cases, warnings).

    Raises OpenAI errors (RateLimitError, APITimeoutError, APIError) to caller.
    """
    # Input caps
    prd = prd[:_MAX_INPUT_CHARS]
    srs = srs[:_MAX_INPUT_CHARS]
    if screenshots:
        screenshots = [s for s in screenshots[:_MAX_SCREENSHOTS] if len(s) <= _MAX_SCREENSHOT_B64_LEN]

    warnings: list[str] = []
    client = OpenAI(api_key=api_key, timeout=120.0)

    system_content = SYSTEM_PROMPT
    if project_name:
        system_content += f"\nProject: {project_name}"
    if project_description:
        system_content += f"\nProject description: {project_description}"
    if base_url:
        system_content += f"\nBase URL: {base_url}"

    if test_data_keys:
        system_content += (
            f"\n\nAvailable test data variables (injected at runtime): {', '.join(test_data_keys)}"
            "\nWhen steps need these values, reference them as 'the <key> from test data' "
            "(e.g., 'Enter the email from test data'). Do NOT invent or hardcode values for these fields."
        )

    if global_setup:
        system_content += (
            f"\n\nProject Setup Context:\n{global_setup}"
            "\n\nUse the setup context above to understand how users access and authenticate with "
            "this application. Every test case you generate MUST begin with the appropriate setup "
            "steps derived from this context (e.g., login, navigation to the relevant page, etc.). "
            "Interpret the setup instructions and translate them into concrete, sequential test steps. "
            "Do NOT skip setup — every test case must be fully self-contained and start from a clean state."
            "\n\nRules for setup steps:"
            "\n- NEVER hardcode credentials, URLs, or environment-specific values in step actions."
            "\n- Reference them generically: 'from test data', 'from prerequisites', 'the configured URL'."
            "\n- Keep each step atomic — one action per step."
            "\n- After setup steps, continue with test-specific verification steps."
        )

    # Build user message content (supports multimodal with screenshots)
    user_content: list[dict] = []
    has_images = bool(screenshots)

    # Add screenshots if provided (base64 data URLs)
    if screenshots:
        for data_url in screenshots:
            user_content.append({
                "type": "image_url",
                "image_url": {"url": data_url},
            })

    # Build text prompt with all available inputs
    prompt_parts = ["Generate test cases from the following inputs:\n"]

    if user_story.strip():
        prompt_parts.append(f"## User Story\n\n{user_story}")

    if prd:
        prompt_parts.append(f"\n\n## PRD (Product Requirements Document)\n\n{prd}")

    if srs:
        prompt_parts.append(f"\n\n## SRS (Software Requirements Specification)\n\n{srs}")

    if existing_test_cases:
        prompt_parts.append(
            f"\n\n## Existing Test Cases (expand coverage, avoid duplication)\n\n{existing_test_cases[:8000]}"
        )

    if has_images:
        prompt_parts.append(
            "\n\n## UI Screenshots\n\nScreenshots of the application UI are attached above. "
            "Use them to identify visible labels, buttons, fields, and UI states."
        )

    prompt_text = "\n".join(prompt_parts)
    user_content.append({"type": "text", "text": prompt_text})

    # Use simple string content when no images
    messages_content: str | list[dict] = (
        user_content if has_images else prompt_text
    )

    # Let OpenAI errors propagate to caller for proper HTTP status mapping
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=16384,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": messages_content},
        ],
    )

    # Token usage logging
    finish_reason = response.choices[0].finish_reason
    if response.usage:
        logger.info(
            "ai_generator.token_usage",
            prompt_tokens=response.usage.prompt_tokens,
            completion_tokens=response.usage.completion_tokens,
            finish_reason=finish_reason,
            model=model,
        )

    raw = response.choices[0].message.content
    if not raw:
        logger.warning("ai_generator.empty_response", finish_reason=finish_reason)
        return [], ["AI returned empty response — the PRD may be too large. Try providing a shorter summary."]

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("ai_generator.invalid_json", raw=raw[:500])
        return [], ["AI returned invalid JSON"]

    test_cases = data.get("test_cases", [])
    if not isinstance(test_cases, list):
        logger.error("ai_generator.bad_structure", keys=list(data.keys()))
        return [], ["AI returned unexpected structure"]

    # Normalize and validate each test case
    normalized = []
    dropped = 0
    for tc in test_cases:
        if not isinstance(tc, dict) or not tc.get("title") or not tc.get("steps"):
            dropped += 1
            continue

        entry = {
            "title": str(tc["title"])[:200],
            "description": str(tc.get("description", "")),
            "test_type": tc.get("test_type", "ui") if tc.get("test_type") in ("ui", "api") else "ui",
            "prerequisites": str(tc.get("prerequisites", "")),
            "priority": tc.get("priority", "medium") if tc.get("priority") in ("low", "medium", "high", "critical") else "medium",
            "tags": [str(t) for t in tc.get("tags", []) if isinstance(t, str)][:10],
            "steps": [],
        }

        for i, step in enumerate(tc.get("steps", []), start=1):
            if not isinstance(step, dict):
                continue
            s = {
                "step_number": i,
                "action": str(step.get("action", "")),
                "expected": str(step.get("expected", "")),
            }
            if entry["test_type"] == "api" and isinstance(step.get("api_config"), dict):
                ac = step["api_config"]
                s["api_config"] = {
                    "method": ac.get("method", "GET"),
                    "endpoint": str(ac.get("endpoint", "")),
                    "headers": ac.get("headers", {}),
                    "body": str(ac.get("body", "")),
                    "expected_status_code": ac.get("expected_status_code"),
                    "expected_response": str(ac.get("expected_response", "")),
                    "extract_vars": ac.get("extract_vars", {}),
                }
            entry["steps"].append(s)

        if not entry["steps"]:
            dropped += 1
            continue

        # Post-generation validation warnings
        step_count = len(entry["steps"])
        if step_count < 2:
            warnings.append(f"Test case '{entry['title'][:50]}' has only {step_count} step(s)")
        elif step_count > 15:
            warnings.append(f"Test case '{entry['title'][:50]}' has {step_count} steps (consider splitting)")
        if not entry["tags"]:
            warnings.append(f"Test case '{entry['title'][:50]}' has no tags")

        normalized.append(entry)

    if dropped:
        warnings.append(f"{dropped} test case(s) dropped due to missing title or steps")

    logger.info("ai_generator.success", count=len(normalized), dropped=dropped)
    return normalized, warnings
