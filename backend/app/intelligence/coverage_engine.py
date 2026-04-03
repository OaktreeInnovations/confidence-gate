"""Infers test coverage breadth and depth from execution telemetry."""

from __future__ import annotations

import structlog

logger = structlog.get_logger(__name__)


def compute_coverage(telemetry_docs: dict) -> dict:
    if not telemetry_docs:
        # 8.5 Cold-start: no telemetry yet — return None score with zero penalty
        # so new projects are not penalized for not having execution history.
        return {
            "coverage_score": None,
            "coverage_penalty": 0.0,
            "url_diversity": 0,
            "action_diversity": 0,
        }

    try:
        unique_urls: set[str] = set()
        unique_actions: set[tuple[str, str]] = set()
        total_steps = 0
        non_navigate_steps = 0

        for doc in telemetry_docs.values():
            steps = doc.get("steps", [])
            total_steps += len(steps)

            for step in steps:
                action_type = str(step.get("action_type", step.get("action", ""))).lower()
                target_key = str(step.get("selector_that_worked", step.get("target", "")))

                # URL diversity: navigate actions
                is_navigate = (
                    "navigate" in action_type
                    or step.get("action_type", "") == "navigate"
                )
                if is_navigate:
                    for attempt in step.get("attempts", []):
                        url = attempt.get("url", attempt.get("target_url", ""))
                        if url:
                            unique_urls.add(url)
                    # Also check step-level url field
                    url = step.get("url", step.get("target_url", ""))
                    if url:
                        unique_urls.add(url)
                else:
                    non_navigate_steps += 1

                unique_actions.add((action_type, target_key))

        # 1. URL diversity — scale denominator to suite size so large suites
        #    are not penalized for already saturating the old fixed threshold.
        url_threshold = max(5, int(total_steps * 0.1))
        url_div_score = min(1.0, len(unique_urls) / url_threshold)

        # 2. Action diversity — same proportional scaling
        action_threshold = max(15, int(total_steps * 0.3))
        action_div_score = min(1.0, len(unique_actions) / action_threshold)

        # 3. Flow depth — average steps per test
        num_docs = len(telemetry_docs)
        avg_steps = total_steps / num_docs if num_docs > 0 else 0
        flow_depth_score = min(1.0, avg_steps / 10.0)

        # 4. Interaction variety — fraction of non-navigate steps
        interaction_variety = non_navigate_steps / total_steps if total_steps > 0 else 0.0

        coverage_score = int(
            ((url_div_score + action_div_score + flow_depth_score + interaction_variety) / 4.0) * 100
        )

        coverage_penalty = (
            0.0
            if coverage_score >= 60
            else round((60 - coverage_score) / 60.0 * 10.0, 4)
        )

        return {
            "coverage_score": coverage_score,
            "coverage_penalty": coverage_penalty,
            "url_diversity": len(unique_urls),
            "action_diversity": len(unique_actions),
        }

    except Exception as e:
        logger.warning("coverage_engine.error", error=str(e)[:200])
        return {
            "coverage_score": 0,
            "coverage_penalty": 10.0,
            "url_diversity": 0,
            "action_diversity": 0,
        }
