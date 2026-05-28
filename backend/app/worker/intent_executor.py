"""Deterministic intent executor — maps ActionIntent to existing wrappers.

This is the core of the structured execution engine. Each ActionType maps 1:1
to an existing deterministic wrapper (safe_click, safe_fill, etc.) or a direct
Playwright call.

Key design:
- Unified wait budget: single time budget per step, no cascading waits
- Scorer-based selector resolution via selector_engine (no AI by default)
- Assertions use Playwright expect() for DOM validation
- All actions track time spent and abort on budget exceeded
- DOM proximity: each resolved element feeds context to the next action
"""

from __future__ import annotations

import json as _json
import time
from typing import TYPE_CHECKING

import structlog

from app.worker.intent_schema import (
    ActionIntent,
    ActionType,
    StepIntent,
    _TARGETLESS_ACTIONS,
)
from app.worker.behavior_detection import capture_page_snapshot, detect_behavior_effect
from app.worker.dom_revalidation import capture_element_identity, revalidate_before_action
from app.worker.page_stability import wait_for_dom_stable
from app.worker.healing.action_repair import repair_action
from app.worker.interaction_discovery import discover_clickable_ancestor
from app.worker.mutation_watcher import start_mutation_watcher, collect_mutations
from app.worker.interaction_guard import ensure_interactable
from app.worker.post_action_verify import (
    ActionVerification,
    capture_pre_state,
    verify_check,
    verify_click,
    verify_fill,
    verify_navigate,
    verify_select,
    verify_select_date,
    verify_uncheck,
    verify_upload,
)
from app.worker.selector_engine import resolve_target, ResolvedTarget
from app.worker.selector_resilience import SelectorStrategy
from app.worker.stability_wrappers import _budget_sleep, _remaining_ms, safe_click, safe_fill, safe_goto, safe_select
from app.worker.component_helpers import resolve_single
from app.worker.component_registry import component_registry
from app.worker.state_awareness import is_action_already_satisfied

if TYPE_CHECKING:
    from app.worker.ai_provider import AIProvider
    from app.worker.in_run_memory import InRunSelectorMemory
    from app.worker.intent_schema import TargetDescriptor
    from app.worker.shared_context import SharedExecutionContext
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)

# Hard cap per step — never exceed regardless of budget config
_STEP_HARD_CAP_MS = 30_000


def execute_intent(
    page: Page,
    intent: StepIntent,
    wait_budget_ms: int = 15_000,
    known_good_selectors: dict[int, list[dict]] | None = None,
    ai_client: AIProvider | None = None,
    in_run_memory: InRunSelectorMemory | None = None,
    shared_context: SharedExecutionContext | None = None,
    last_step_selector: str | None = None,
) -> tuple[bool, str, list[dict], dict, str | None]:
    """Execute all actions in a StepIntent sequentially.

    Uses a single time budget. Each action deducts from the budget.
    If budget is exceeded, remaining actions are skipped and the step fails.

    Args:
        page: Playwright Page instance.
        intent: Structured intent with 1+ actions.
        wait_budget_ms: Total time budget for the entire step.
        known_good_selectors: Dict of action_index → known-good selectors.
        ai_client: Optional OpenAI client for AI disambiguation (last resort).
        in_run_memory: Per-run selector memory for blacklisting/promotion.
        last_step_selector: CSS path from the previous step's last action,
            used as initial proximity context for element resolution.

    Returns:
        (success, description, selectors_used, stability_metrics, last_selector) where:
        - success: True if all actions completed without error
        - description: Human-readable summary of what was done
        - selectors_used: List of selector dicts for telemetry
        - stability_metrics: Aggregated stability signals for telemetry
        - last_selector: CSS path of the last resolved element (for cross-step proximity)
    """
    budget_ms = min(wait_budget_ms, _STEP_HARD_CAP_MS, _remaining_ms())
    step_start = time.monotonic()
    selectors_used: list[dict] = []
    descriptions: list[str] = []
    last_action_selector: str | None = last_step_selector
    used_selector_paths: set[str] = set()

    # Stability metrics accumulator (aggregated across all actions)
    max_resolution_tier = 0
    all_guard_failures: list[str] = []
    all_verified = True
    last_verification_method = ""
    ancestor_discovery_count = 0
    behavior_failure_count = 0
    total_interaction_attempts = 0
    resolution_retry_count = 0
    all_behavior_signals: list[str] = []
    total_mutation_count = 0
    any_significant_mutation = False
    any_disambiguation_used = False
    last_disambiguation_layer = ""

    for i, action in enumerate(intent.actions):
        elapsed_ms = int((time.monotonic() - step_start) * 1000)
        remaining_ms = budget_ms - elapsed_ms

        if remaining_ms <= 0:
            raise TimeoutError(
                f"Wait budget exhausted after {elapsed_ms}ms "
                f"({i}/{len(intent.actions)} actions completed)"
            )

        # Per-action timeout: split remaining budget evenly among remaining actions
        actions_left = len(intent.actions) - i
        action_timeout_ms = max(remaining_ms // actions_left, 2000)

        known_good = (known_good_selectors or {}).get(i)

        result: dict = {}
        try:
            result = _execute_single_action(
                page, action, action_timeout_ms, known_good,
                last_action_selector=last_action_selector,
                ai_client=ai_client,
                in_run_memory=in_run_memory,
                action_index=i,
                used_selector_paths=frozenset(used_selector_paths),
            )
        except (RuntimeError, TimeoutError) as exc:
            # Record selector failure if we have memory and selector key
            if in_run_memory and result.get("selector_key"):
                in_run_memory.record_failure(i, result["selector_key"])

            # Soft-skip: if a click action fails and the NEXT action's
            # precondition is already satisfied on the page, the click was
            # likely a redundant "reveal section" action.  Only skip when we
            # can confirm the next action can proceed without it.
            _skippable_error = (
                "All candidates blacklisted" in str(exc)
                or "No element found" in str(exc)
            )
            if (
                action.action == ActionType.CLICK
                and _skippable_error
                and i < len(intent.actions) - 1
            ):
                next_action = intent.actions[i + 1]
                can_skip = False
                # If the next action is wait_for_text, check if the text
                # is already visible — if so the click was unnecessary
                if (
                    next_action.action == ActionType.WAIT_FOR_TEXT
                    and next_action.value
                ):
                    try:
                        loc = page.get_by_text(next_action.value).first
                        loc.wait_for(state="visible", timeout=1000)
                        can_skip = True
                    except Exception:
                        try:
                            loc = page.get_by_placeholder(
                                next_action.value
                            ).first
                            loc.wait_for(state="visible", timeout=500)
                            can_skip = True
                        except Exception:
                            can_skip = False
                # If the next action targets a specific element, check if
                # that element is already visible on the page
                elif next_action.target and (
                    next_action.action in (
                        ActionType.CLICK, ActionType.INPUT,
                        ActionType.SELECT,
                    )
                ):
                    try:
                        t = next_action.target
                        if t.placeholder:
                            loc = page.get_by_placeholder(t.placeholder).first
                        elif t.label:
                            loc = page.get_by_label(t.label).first
                        elif t.text:
                            loc = page.get_by_text(t.text).first
                        else:
                            loc = None
                        if loc:
                            loc.wait_for(state="visible", timeout=1000)
                            can_skip = True
                    except Exception:
                        can_skip = False

                if can_skip:
                    logger.info(
                        "intent_executor.click_soft_skipped",
                        action_index=i,
                        target=str(action.target)[:80] if action.target else "",
                        reason="next_action_already_satisfied",
                    )
                    descriptions.append(
                        f"Skipped click (target not interactive): "
                        f"{action.target}"
                    )
                    continue

            # Enrich error with completed actions so regeneration knows
            # which actions already succeeded and should not be repeated
            if descriptions:
                completed = "; ".join(
                    f"[{j}] {d}" for j, d in enumerate(descriptions)
                )
                raise type(exc)(
                    f"{exc} | COMPLETED_ACTIONS: {completed}"
                ) from exc
            raise

        # Resolution retry: if the action clearly targeted the wrong element,
        # blacklist the selector and re-resolve once before falling back to
        # the expensive step-level retry. Two trigger paths:
        #   (a) No behavior effect AND repair didn't help → element is inert
        #   (b) Behavior effect detected BUT verification failed → wrong element
        _RESOLUTION_RETRY_ACTIONS = frozenset({
            ActionType.CLICK, ActionType.INPUT, ActionType.SELECT,
        })
        behavior = result.get("behavior_effect")
        verification = result.get("action_verification")
        _no_effect_no_repair = (
            behavior is not None
            and not behavior.detected
            and result.get("behavior_retry_method") is None
        )
        _effect_but_unverified = (
            behavior is not None
            and behavior.detected
            and verification is not None
            and not verification.verified
        )
        # Skip resolution retry when behavior detector or mutation watcher
        # confirmed the click had an effect — re-resolving would double-click
        _mutation = result.get("mutation_summary")
        _behavior_confirmed_effect = (
            (behavior is not None and behavior.detected)
            or (_mutation is not None and _mutation.has_significant_mutations)
        )
        if (
            ((_no_effect_no_repair and not _behavior_confirmed_effect) or _effect_but_unverified)
            and result.get("selector_key")
            and in_run_memory is not None
            and action.action in _RESOLUTION_RETRY_ACTIONS
        ):
            old_key = result["selector_key"]
            # Force-blacklist: 2 failures triggers blacklist threshold
            in_run_memory.record_failure(i, old_key)
            in_run_memory.record_failure(i, old_key)
            logger.info(
                "intent_executor.resolution_retry",
                action_index=i,
                blacklisted=old_key[:80],
            )
            retry_elapsed = int((time.monotonic() - step_start) * 1000)
            retry_remaining = budget_ms - retry_elapsed
            if retry_remaining > 2000:
                try:
                    retry_timeout = max(retry_remaining // actions_left, 2000)
                    retry_result = _execute_single_action(
                        page, action, retry_timeout, known_good,
                        last_action_selector=last_action_selector,
                        ai_client=ai_client,
                        in_run_memory=in_run_memory,
                        action_index=i,
                        used_selector_paths=frozenset(used_selector_paths),
                    )
                    # Check if retry improved: effect detected, repair worked,
                    # or verification now passes
                    retry_beh = retry_result.get("behavior_effect")
                    retry_ver = retry_result.get("action_verification")
                    if (
                        (retry_beh is not None and retry_beh.detected)
                        or retry_result.get("behavior_retry_method")
                        or (retry_ver is not None and retry_ver.verified)
                    ):
                        result = retry_result
                        result["resolution_retried"] = True
                        resolution_retry_count += 1
                        logger.info(
                            "intent_executor.resolution_retry_success",
                            action_index=i,
                        )
                    else:
                        # Re-resolution didn't help either; keep original result
                        resolution_retry_count += 1
                        logger.info(
                            "intent_executor.resolution_retry_no_improvement",
                            action_index=i,
                        )
                except (RuntimeError, TimeoutError) as e:
                    resolution_retry_count += 1
                    logger.debug(
                        "intent_executor.resolution_retry_failed",
                        action_index=i,
                        error=str(e)[:150],
                    )

        # Record selector success
        if in_run_memory and result.get("selector_key"):
            in_run_memory.record_success(i, result["selector_key"])

        if result.get("selector_info"):
            selectors_used.append(result["selector_info"])
            # Track highest resolution tier used
            tier = result["selector_info"].get("resolution_tier", 0)
            if tier > max_resolution_tier:
                max_resolution_tier = tier
        if result.get("selector_path"):
            last_action_selector = result["selector_path"]
            used_selector_paths.add(result["selector_path"])
        if result.get("description"):
            descriptions.append(result["description"])
        # Store extracted variables in shared context
        if shared_context and result.get("extracted_variable"):
            for var_name, var_value in result["extracted_variable"].items():
                shared_context.set_variable(var_name, var_value)

        # Collect guard failures
        guard = result.get("guard_result")
        if guard is not None and guard.checks_failed:
            all_guard_failures.extend(guard.checks_failed)

        # Collect verification results
        verification = result.get("action_verification")
        if verification is not None:
            if not verification.verified:
                all_verified = False
            last_verification_method = verification.method

        # Track clickable ancestor discovery
        if result.get("clickable_ancestor_used"):
            ancestor_discovery_count += 1

        # Track behavior effect failures, signals, and interaction attempts
        behavior = result.get("behavior_effect")
        if behavior is not None:
            if not behavior.detected:
                behavior_failure_count += 1
            for sig in behavior.signals:
                if sig not in all_behavior_signals:
                    all_behavior_signals.append(sig)
        total_interaction_attempts += result.get("interaction_attempts", 1)

        # Track disambiguation usage
        sel_info = result.get("selector_info") or {}
        if sel_info.get("disambiguation"):
            any_disambiguation_used = True
            last_disambiguation_layer = sel_info.get("disambiguation_layer", "") or last_disambiguation_layer

        # Track mutation data
        mut = result.get("mutation_summary")
        if mut is not None:
            total_mutation_count += mut.total_mutations
            if mut.has_significant_mutations:
                any_significant_mutation = True

        # Extended DOM stability wait after aria-expanded toggle.
        # Sidebar/accordion expand animations need 300-500ms for submenu
        # content to render.  The standard 1500ms/150ms settle inside
        # _execute_single_action is too short.  Wait for DOM to stabilize
        # before the next action tries to find the newly-revealed element.
        if (
            action.action == ActionType.CLICK
            and behavior is not None
            and "aria_expanded" in behavior.signals
        ):
            try:
                wait_for_dom_stable(page, timeout_ms=3000, settle_ms=300)
            except Exception:
                pass
            logger.debug("intent_executor.wait_after_expand", action_index=i)

        # Extended wait after click that triggered URL change (SPA navigation).
        # Client-side routers (Next.js, React Router) change the URL via
        # pushState, but content may not be rendered yet.  Use lightweight
        # domcontentloaded (not networkidle — can stall on long-polling /
        # websockets) and short DOM settle to avoid eating the step budget.
        if (
            action.action == ActionType.CLICK
            and behavior is not None
            and "url_change" in behavior.signals
        ):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            try:
                wait_for_dom_stable(page, timeout_ms=3000, settle_ms=500)
            except Exception:
                pass
            logger.debug("intent_executor.wait_after_nav_click", action_index=i)

        # Retry navigation click when url_change did NOT fire.
        # Anchor elements (<a href="...">) in SPAs sometimes fail to navigate
        # on the first click (event handler not attached, async rendering,
        # pointer-events).  Detect by checking if the resolved DOM element
        # (or its closest <a> ancestor) has an href attribute, then retry
        # with JS click + href fallback.
        if (
            action.action == ActionType.CLICK
            and behavior is not None
            and "url_change" not in behavior.signals
            and i > 0
        ):
            locator_for_retry = result.get("locator")
            if locator_for_retry is not None:
                try:
                    retry_href = locator_for_retry.get_attribute("href")
                except Exception:
                    retry_href = None
                # If element itself has no href, check closest <a> ancestor
                if not retry_href:
                    try:
                        retry_href = locator_for_retry.evaluate(
                            "el => el.closest('a')?.href || ''"
                        ) or None
                    except Exception:
                        retry_href = None
                if retry_href:
                    pre_url = page.url
                    import time as _time
                    for retry in range(2):
                        try:
                            locator_for_retry.evaluate("""el => {
                                var link = el.closest('a') || el;
                                link.scrollIntoView({block: 'center'});
                                link.click();
                            }""")
                            _budget_sleep(0.5, "link_nav_js_click_settle")
                            if page.url != pre_url:
                                logger.info(
                                    "intent_executor.link_nav_retry_success",
                                    action_index=i,
                                    retry=retry + 1,
                                    method="js_click",
                                )
                                try:
                                    page.wait_for_load_state("domcontentloaded", timeout=3000)
                                except Exception:
                                    pass
                                try:
                                    wait_for_dom_stable(page, timeout_ms=3000, settle_ms=500)
                                except Exception:
                                    pass
                                break
                        except Exception:
                            pass
                    else:
                        try:
                            page.evaluate(f"() => window.location.href = '{retry_href}'")
                            _budget_sleep(1.0, "link_nav_href_settle")
                            if page.url != pre_url:
                                logger.info(
                                    "intent_executor.link_nav_retry_success",
                                    action_index=i,
                                    method="href_navigate",
                                )
                                try:
                                    page.wait_for_load_state("domcontentloaded", timeout=3000)
                                except Exception:
                                    pass
                                try:
                                    wait_for_dom_stable(page, timeout_ms=3000, settle_ms=500)
                                except Exception:
                                    pass
                        except Exception:
                            pass

    summary = intent.description or "; ".join(descriptions) or "Actions executed"
    stability_metrics = {
        "resolution_tier": max_resolution_tier,
        "guard_failures": all_guard_failures,
        "post_action_verified": all_verified,
        "post_action_method": last_verification_method,
        "clickable_ancestor_used": ancestor_discovery_count,
        "behavior_failures": behavior_failure_count,
        "interaction_attempts": total_interaction_attempts,
        "resolution_retries": resolution_retry_count,
        "behavior_signals": all_behavior_signals,
        "mutation_total": total_mutation_count,
        "mutation_significant": any_significant_mutation,
        "disambiguation_used": any_disambiguation_used,
        "disambiguation_layer": last_disambiguation_layer,
    }
    return True, summary, selectors_used, stability_metrics, last_action_selector


def _execute_single_action(
    page: Page,
    action: ActionIntent,
    timeout_ms: int,
    known_good: list[dict] | None = None,
    last_action_selector: str | None = None,
    ai_client: AIProvider | None = None,
    in_run_memory: InRunSelectorMemory | None = None,
    action_index: int = 0,
    used_selector_paths: frozenset[str] | None = None,
) -> dict:
    """Execute one ActionIntent by dispatching to the appropriate wrapper.

    Args:
        page: Playwright Page instance.
        action: Single action to execute.
        timeout_ms: Time budget for this action.
        known_good: Known-good selectors to try first.
        last_action_selector: CSS path of last interacted element for proximity.
        ai_client: Optional OpenAI client for disambiguation.
        in_run_memory: Per-run selector memory for blacklisting/promotion.
        action_index: Index of this action within the step (for memory keying).

    Returns:
        Dict with optional keys: selector_info, description, selector_path.
    """
    result: dict = {}
    locator = None
    strategy = None

    # State-aware: skip if action's goal is already satisfied
    if is_action_already_satisfied(page, action):
        logger.info(
            "intent_executor.skipped_satisfied",
            action=action.action.value,
            value=(action.value or "")[:50],
        )
        result["description"] = f"Skipped (already satisfied): {action.action.value}"
        return result

    # Resolve target element for actions that need one
    # SELECT and SELECT_DATE have their own auto-discovery fallbacks via component
    # strategies, so resolution failure is non-fatal for those action types.
    _COMPONENT_STRATEGY_ACTIONS = frozenset({ActionType.SELECT, ActionType.SELECT_DATE})

    if action.action not in _TARGETLESS_ACTIONS and action.target and not action.target.is_empty():
        # Check in-run memory for preferred selector — inject as known-good hint
        preferred = in_run_memory.get_preferred(action_index) if in_run_memory else None
        if preferred:
            promoted_hint = _parse_selector_key(preferred)
            if promoted_hint and _preferred_matches_target(promoted_hint, action.target):
                logger.debug(
                    "intent_executor.using_preferred_selector",
                    action_index=action_index,
                    selector=preferred[:80],
                )
                known_good = [promoted_hint] + (known_good or [])
            elif promoted_hint:
                logger.debug(
                    "intent_executor.preferred_selector_rejected",
                    action_index=action_index,
                    preferred=preferred[:80],
                    target_label=action.target.label or "",
                    target_name=action.target.name or "",
                )

        try:
            resolved = resolve_target(
                page, action.target,
                timeout_ms=min(timeout_ms, 10_000),
                known_good_selectors=known_good,
                last_action_selector=last_action_selector,
                ai_client=ai_client,
                action_description=f"{action.action.value} {action.value or ''}".strip(),
                in_run_memory=in_run_memory,
                action_index=action_index,
                used_selector_paths=used_selector_paths,
            )
            locator = resolved.locator
            strategy = resolved.strategy
            result["selector_info"] = resolved.to_telemetry()
            result["selector_path"] = resolved.selector_path
            # Key for in-run memory (matches blacklist format in resolver)
            result["selector_key"] = f"{resolved.score.strategy.value}:{resolved.score.params}"
            # Capture element identity for DOM revalidation
            result["_element_identity"] = capture_element_identity(locator)
            result["_target"] = action.target
        except (RuntimeError, TimeoutError) as e:
            if action.action in _COMPONENT_STRATEGY_ACTIONS:
                # Let component strategy handle discovery
                logger.info(
                    "intent_executor.resolution_fallback",
                    action=action.action.value,
                    error=str(e)[:150],
                )
            else:
                raise

    # Clickable element discovery: if resolved element is not itself clickable
    # (e.g. a <span> inside a clickable <div>), walk up DOM to find the
    # nearest interactive ancestor. Only for CLICK actions.
    clickable_ancestor_used = False
    if locator is not None and action.action == ActionType.CLICK:
        original_locator = locator
        locator = discover_clickable_ancestor(locator)
        if locator is not original_locator:
            clickable_ancestor_used = True
            result["clickable_ancestor_used"] = True

    # Pre-action interaction guard for interactive actions
    _INTERACTIVE_ACTIONS = frozenset({
        ActionType.CLICK, ActionType.INPUT, ActionType.SELECT,
        ActionType.SELECT_DATE, ActionType.CHECK, ActionType.UNCHECK,
        ActionType.UPLOAD_FILE,
    })
    if locator is not None and action.action in _INTERACTIVE_ACTIONS:
        # Skip overlay check for select_date — the calendar popup IS the
        # expected UI state; elementFromPoint would see the popup, not the
        # trigger field underneath.
        skip_overlay = action.action == ActionType.SELECT_DATE
        guard = ensure_interactable(
            locator,
            max_wait_ms=min(timeout_ms, 3000),
            check_overlay=not skip_overlay,
        )
        if not guard.passed:
            # overlay_detected is a soft failure — safe_click already has
            # JS dispatch fallback for overlays.  Only hard-fail if there
            # are *other* blocking checks (disabled, not visible, etc.).
            hard_failures = [
                c for c in guard.checks_failed if c != "overlay_detected"
            ]
            if hard_failures:
                raise RuntimeError(
                    f"Interaction guard failed for {action.action.value}: "
                    f"failed checks: {guard.checks_failed}"
                )
            # Overlay-only failure — log and proceed, let safe_click handle it
            logger.info(
                "intent_executor.overlay_soft_pass",
                action=action.action.value,
                target=str(action.target)[:80] if action.target else "",
            )
            guard.passed = True
        result["guard_result"] = guard

    # DOM revalidation: re-resolve if element was replaced between resolve and action
    if locator is not None and result.get("_element_identity") and result.get("_target"):
        locator = revalidate_before_action(
            page, locator, result["_element_identity"], result["_target"],
            timeout_ms=min(timeout_ms, 5000),
            known_good_selectors=known_good,
            last_action_selector=last_action_selector,
            ai_client=ai_client,
        )

    # Pre-action state capture for post-action verification
    pre_state = capture_pre_state(page, locator, action.action.value)

    # Behavior effect detection: capture page snapshot before action
    _BEHAVIOR_TRACKED_ACTIONS = frozenset({
        ActionType.CLICK, ActionType.INPUT, ActionType.SELECT,
        ActionType.SELECT_DATE, ActionType.CHECK, ActionType.UNCHECK,
        ActionType.NAVIGATE,
    })
    page_snapshot = None
    if action.action in _BEHAVIOR_TRACKED_ACTIONS:
        try:
            page_snapshot = capture_page_snapshot(page, locator)
        except Exception:
            pass

    # Start DOM mutation watcher before action dispatch
    watcher_active = False
    if page_snapshot is not None:
        watcher_active = start_mutation_watcher(page)

    # Dispatch to appropriate wrapper
    if action.action == ActionType.NAVIGATE:
        url = action.value or ""
        if not url:
            raise ValueError("navigate action requires a 'value' (URL)")
        # Resolve relative URLs against the current page origin
        if url.startswith("/"):
            try:
                from urllib.parse import urlparse
                parsed = urlparse(page.url)
                url = f"{parsed.scheme}://{parsed.netloc}{url}"
            except Exception:
                pass
        safe_goto(page, url, timeout_ms=max(timeout_ms, 30000))
        result["description"] = f"Navigated to {url}"

    elif action.action == ActionType.INPUT:
        if locator is None:
            raise RuntimeError("input action requires a target element")
        # Detect contenteditable — fill() doesn't work on these
        is_contenteditable = False
        try:
            ce_attr = locator.get_attribute("contenteditable")
            is_contenteditable = ce_attr in ("true", "")
        except Exception:
            pass
        if is_contenteditable:
            locator.click()
            locator.evaluate("el => { el.textContent = ''; }")
            locator.press_sequentially(action.value or "", delay=30)
            result["description"] = f"Typed into contenteditable: '{(action.value or '')[:50]}'"
        else:
            safe_fill(locator, action.value or "", timeout_ms=timeout_ms)
            result["description"] = f"Filled input with '{(action.value or '')[:50]}'"

    elif action.action == ActionType.CLICK:
        if locator is None:
            raise RuntimeError("click action requires a target element")
        # If the guard detected pointer-events: none, use JS dispatch directly
        # because native click() would pass through to the element below.
        guard = result.get("guard_result")
        if guard and "pointer_events_none_bypassed" in (guard.remediation_applied or []):
            logger.info("intent_executor.js_click_pointer_events_bypass")
            locator.evaluate("""el => {
                const opts = {bubbles: true, cancelable: true, pointerId: 1};
                el.dispatchEvent(new PointerEvent('pointerdown', opts));
                el.dispatchEvent(new PointerEvent('pointerup', opts));
                el.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            }""")
        else:
            safe_click(locator, timeout_ms=timeout_ms)
        result["description"] = f"Clicked element"
        result["locator"] = locator  # Stored for retry logic in execute_intent

    elif action.action == ActionType.SELECT:
        strategies = component_registry.get_strategies(page)
        option_text = action.value or ""
        if action.target and action.target.label:
            # Best case: target has a label → auto-discover dropdown by label
            strategies.select_dropdown(page, action.target.label, option_text)
        elif locator is not None:
            safe_select(locator, option_text, timeout_ms=timeout_ms)
        elif action.target and action.target.name:
            # AI targeted the option (role='option') or used name —
            # fall back to using name as dropdown label for auto-discovery
            logger.info(
                "intent_executor.select_fallback",
                name=action.target.name,
                option=option_text,
            )
            strategies.select_dropdown(page, action.target.name, option_text or action.target.name)
        elif action.target and action.target.text:
            strategies.select_dropdown(page, action.target.text, option_text or action.target.text)
        else:
            raise RuntimeError("select action requires a target element or label")
        result["description"] = f"Selected '{(option_text)[:50]}'"

    elif action.action == ActionType.SELECT_DATE:
        strategies = component_registry.get_strategies(page)
        trigger = action.target.label if action.target and action.target.label else ""
        if not trigger and locator is not None:
            trigger = locator
        if not trigger and action.target:
            # Fallback: use name or text as trigger label for auto-discovery
            fallback_label = action.target.name or action.target.text or ""
            if fallback_label:
                logger.info(
                    "intent_executor.select_date_fallback",
                    name=action.target.name,
                    text=action.target.text,
                )
                trigger = fallback_label
        if not trigger:
            raise RuntimeError("select_date action requires a target label or locator")
        day = action.day or action.value or ""
        if not day:
            raise ValueError("select_date action requires 'day' or 'value'")
        strategies.pick_date(
            page, trigger, day,
            month_label=action.month_label or "",
        )
        result["description"] = f"Selected date: day={day}"

    elif action.action == ActionType.UPLOAD_FILE:
        strategies = component_registry.get_strategies(page)
        file_path = action.value or ""
        if not file_path:
            raise ValueError("upload_file action requires a 'value' (file path)")
        strategies.upload_file(page, file_path, trigger=locator)
        result["description"] = f"Uploaded file: {file_path}"

    elif action.action == ActionType.CHECK:
        if locator is None:
            raise RuntimeError("check action requires a target element")
        _scroll_and_check(locator, timeout_ms, check=True)
        result["description"] = "Checked checkbox"

    elif action.action == ActionType.UNCHECK:
        if locator is None:
            raise RuntimeError("uncheck action requires a target element")
        _scroll_and_check(locator, timeout_ms, check=False)
        result["description"] = "Unchecked checkbox"

    elif action.action == ActionType.WAIT_FOR_TEXT:
        text = action.value or ""
        if not text:
            raise ValueError("wait_for_text action requires a 'value' (text)")
        # Try visible text first, fall back to placeholder attribute
        text_loc = page.get_by_text(text)
        if text_loc.count() > 0:
            resolve_single(
                text_loc, context=f"wait_for_text:{text[:30]}",
                timeout_ms=timeout_ms,
            ).wait_for(state="visible", timeout=timeout_ms)
        else:
            placeholder_loc = page.get_by_placeholder(text)
            if placeholder_loc.count() > 0:
                resolve_single(
                    placeholder_loc,
                    context=f"wait_for_placeholder:{text[:30]}",
                    timeout_ms=timeout_ms,
                ).wait_for(state="visible", timeout=timeout_ms)
            else:
                # Neither found — use original locator to get the standard error
                resolve_single(
                    text_loc, context=f"wait_for_text:{text[:30]}",
                    timeout_ms=timeout_ms,
                ).wait_for(state="visible", timeout=timeout_ms)
        result["description"] = f"Waited for text: '{text[:50]}'"

    elif action.action == ActionType.WAIT_FOR_NAVIGATION:
        url_pattern = action.value or ""
        if not url_pattern:
            raise ValueError("wait_for_navigation action requires a 'value' (URL pattern)")
        pre_nav_url = page.url
        navigated = False

        # Normalize glob: AI often generates "*/" which only matches one
        # segment in Playwright.  Convert leading * to ** so it crosses
        # path separators (e.g. "*/sign-in*" → "**/sign-in*").
        pw_pattern = url_pattern
        if pw_pattern.startswith("*/") and not pw_pattern.startswith("**/"):
            pw_pattern = "**/" + pw_pattern[2:]

        try:
            # Use domcontentloaded instead of load — SPAs often never fire load
            page.wait_for_url(
                pw_pattern, timeout=timeout_ms,
                wait_until="domcontentloaded",
            )
            navigated = True
        except Exception:
            # Check if URL changed despite timeout (slow load event)
            if page.url != pre_nav_url:
                navigated = True

        if not navigated:
            # Lenient match: Playwright glob may be too strict.  Fall back
            # to Python fnmatch (where * crosses /) to check if the current
            # URL already satisfies the pattern.
            import fnmatch
            if fnmatch.fnmatch(page.url, url_pattern):
                navigated = True

        if not navigated:
            # SPA fallback: many apps do AJAX login then JS redirect.
            # Wait for network to settle, then poll for URL change.
            import time as _time
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=min(timeout_ms, 10_000),
                )
            except Exception:
                pass
            if page.url != pre_nav_url:
                navigated = True
            else:
                # Final attempt: short poll for delayed JS redirect
                deadline = _time.monotonic() + min(timeout_ms / 1000, 5.0)
                while _time.monotonic() < deadline:
                    _time.sleep(0.5)
                    if page.url != pre_nav_url:
                        navigated = True
                        break

        if not navigated:
            # Already-navigated: if the current URL does NOT match the
            # pattern, navigation already occurred in a prior action
            # (e.g. form auto-submit on password entry moved us past
            # the source page).
            import fnmatch
            if not fnmatch.fnmatch(page.url, url_pattern):
                navigated = True
                logger.info(
                    "intent_executor.navigation_already_occurred",
                    pattern=url_pattern,
                    current_url=page.url,
                )

        if not navigated:
            raise TimeoutError(
                f"Navigation did not occur: URL unchanged at {pre_nav_url}"
            )
        result["description"] = f"Navigated from {pre_nav_url} to {page.url}"

    elif action.action == ActionType.ASSERT_TEXT:
        if locator is None:
            raise RuntimeError("assert_text action requires a target element")
        from playwright.sync_api import expect
        expect(locator).to_have_text(action.value or "", timeout=timeout_ms)
        result["description"] = f"Asserted text: '{(action.value or '')[:50]}'"

    elif action.action == ActionType.ASSERT_VISIBLE:
        if locator is None:
            raise RuntimeError("assert_visible action requires a target element")
        from playwright.sync_api import expect
        expect(locator).to_be_visible(timeout=timeout_ms)
        result["description"] = "Asserted element visible"

    elif action.action == ActionType.ASSERT_VALUE:
        if locator is None:
            raise RuntimeError("assert_value action requires a target element")
        from playwright.sync_api import expect
        expected_val = action.value or ""
        try:
            expect(locator).to_have_value(expected_val, timeout=timeout_ms)
        except Exception:
            # Fallback for custom comboboxes/selects that aren't native inputs:
            # check inner text, data-value, or aria-valuenow contains the expected value
            text = locator.inner_text(timeout=3000).strip()
            if expected_val.lower() not in text.lower():
                data_val = locator.get_attribute("data-value") or ""
                aria_val = locator.get_attribute("aria-valuenow") or ""
                if expected_val.lower() not in data_val.lower() and expected_val.lower() not in aria_val.lower():
                    raise RuntimeError(
                        f"Element text '{text[:80]}' does not contain "
                        f"expected value '{expected_val[:80]}'"
                    )
        result["description"] = f"Asserted value: '{expected_val[:50]}'"

    elif action.action == ActionType.EXTRACT_VALUE:
        if locator is None:
            raise RuntimeError("extract_value action requires a target element")
        var_name = action.value or ""
        if not var_name:
            raise ValueError("extract_value action requires 'value' (variable name)")
        # Extract text content or input value
        try:
            extracted = locator.input_value(timeout=timeout_ms)
        except Exception:
            extracted = locator.inner_text(timeout=timeout_ms)
        result["extracted_variable"] = {var_name: extracted}
        result["description"] = f"Extracted '{var_name}' = '{extracted[:50]}'"

    else:
        raise ValueError(f"Unknown action type: {action.action}")

    # Collect DOM mutations recorded during action dispatch
    mutation_summary = None
    if watcher_active:
        mutation_summary = collect_mutations(page)
        result["mutation_summary"] = mutation_summary

    # Brief DOM stability wait before behavior detection — let mutations settle
    # so the post-action snapshot reflects the final state, not an intermediate one.
    if page_snapshot is not None:
        try:
            wait_for_dom_stable(page, timeout_ms=1500, settle_ms=300)
        except Exception:
            pass

    # Behavior effect detection: compare page state after action
    if page_snapshot is not None:
        try:
            effect = detect_behavior_effect(page, locator, page_snapshot)
            result["behavior_effect"] = effect
        except Exception:
            pass

    # Mutation watcher supplement: if snapshot-based detection missed the
    # effect but the mutation watcher recorded significant DOM changes,
    # override to detected. This catches structural mutations that don't
    # change innerText (e.g., element insertion without text content).
    if (
        mutation_summary is not None
        and mutation_summary.has_significant_mutations
        and result.get("behavior_effect") is not None
        and not result["behavior_effect"].detected
    ):
        from app.worker.behavior_detection import BehaviorEffect
        result["behavior_effect"] = BehaviorEffect(
            detected=True,
            signals=["mutation_watcher"],
            detail=f"Watcher: {mutation_summary.total_mutations} mutations "
                   f"({mutation_summary.node_insertions} ins, "
                   f"{mutation_summary.node_removals} rem)",
        )
        logger.info(
            "intent_executor.mutation_watcher_override",
            total=mutation_summary.total_mutations,
            insertions=mutation_summary.node_insertions,
        )

    # Behavior-driven retry: when action had no observable effect, try
    # alternate interaction strategies before falling through to verification.
    # Reuses healing/action_repair alternates proactively (not post-failure).
    _BEHAVIOR_RETRY_ACTIONS = frozenset({
        ActionType.CLICK, ActionType.INPUT, ActionType.SELECT,
    })
    behavior_effect = result.get("behavior_effect")
    if (
        behavior_effect is not None
        and not behavior_effect.detected
        and locator is not None
        and action.action in _BEHAVIOR_RETRY_ACTIONS
    ):
        logger.info(
            "intent_executor.behavior_retry",
            action=action.action.value,
        )
        try:
            repair_result = repair_action(
                page, locator, action, pre_state, max_attempts=2,
            )
            result["interaction_attempts"] = 1 + repair_result.attempts
            if repair_result.repaired:
                logger.info(
                    "intent_executor.behavior_retry_success",
                    method=repair_result.method_used,
                    attempts=repair_result.attempts,
                )
                result["behavior_retry_method"] = repair_result.method_used
                # Re-detect behavior effect after successful retry
                try:
                    effect = detect_behavior_effect(page, locator, page_snapshot)
                    result["behavior_effect"] = effect
                except Exception:
                    pass
                # Use repair's verification as the action verification
                if repair_result.verification is not None:
                    result["action_verification"] = repair_result.verification
        except Exception as e:
            logger.debug(
                "intent_executor.behavior_retry_error",
                error=str(e)[:150],
            )

    # Post-action verification (warns on unverified, soft retry for clicks)
    try:
        verification: ActionVerification | None = None
        if action.action == ActionType.CLICK:
            verification = verify_click(page, locator, pre_state)
            # Skip soft retry if behavior detector already saw significant
            # DOM mutations (e.g. wizard step transition, modal content change).
            # Re-clicking would trigger duplicate actions like form validation.
            behavior_effect = result.get("behavior_effect")
            mutation_summary = result.get("mutation_summary")
            behavior_confirmed = (
                (
                    behavior_effect is not None
                    and behavior_effect.detected
                )
                or (
                    mutation_summary is not None
                    and mutation_summary.has_significant_mutations
                )
            )
            # P0 fix: unverified click → soft retry once (re-click + re-verify)
            if verification and not verification.verified and locator is not None and not behavior_confirmed:
                logger.info(
                    "intent_executor.click_soft_retry",
                    detail=verification.detail[:100],
                )
                try:
                    retry_pre_state = capture_pre_state(page, locator, action.action.value)
                    safe_click(locator, timeout_ms=min(timeout_ms, 3000))
                    verification = verify_click(page, locator, retry_pre_state)
                except Exception:
                    pass  # Retry failed — keep original unverified result
        elif action.action == ActionType.INPUT:
            verification = verify_fill(locator, action.value or "")
        elif action.action == ActionType.NAVIGATE:
            verification = verify_navigate(page, action.value or "")
        elif action.action == ActionType.SELECT:
            verification = verify_select(page, locator, action.value or "")
        elif action.action == ActionType.CHECK:
            if locator is not None:
                verification = verify_check(locator)
        elif action.action == ActionType.UNCHECK:
            if locator is not None:
                verification = verify_uncheck(locator)
        elif action.action == ActionType.SELECT_DATE:
            verification = verify_select_date(
                page, locator, action.day or action.value or "",
            )
        elif action.action == ActionType.UPLOAD_FILE:
            verification = verify_upload(page, locator, action.value or "")

        if verification:
            result["action_verification"] = verification
            if not verification.verified:
                logger.info(
                    "intent_executor.post_action_unverified",
                    action=action.action.value,
                    method=verification.method,
                    detail=verification.detail[:100],
                )
    except Exception:
        pass  # Verification failure is non-fatal

    # Unified action feedback — captures the full chain for telemetry
    behavior_effect = result.get("behavior_effect")
    final_verification = result.get("action_verification")
    mut = result.get("mutation_summary")
    result["action_feedback"] = {
        "action": action.action.value,
        "behavior_detected": behavior_effect.detected if behavior_effect else None,
        "behavior_signals": behavior_effect.signals if behavior_effect else [],
        "repair_method": result.get("behavior_retry_method"),
        "verified": final_verification.verified if final_verification else None,
        "verification_method": final_verification.method if final_verification else "",
        "interaction_attempts": result.get("interaction_attempts", 1),
        "ancestor_used": result.get("clickable_ancestor_used", False),
        "mutations_total": mut.total_mutations if mut else 0,
        "mutations_significant": mut.has_significant_mutations if mut else False,
    }

    return result


def _scroll_and_check(
    locator: Locator,
    timeout_ms: int,
    check: bool = True,
) -> None:
    """Scroll into view, wait for visible, then check/uncheck."""
    try:
        locator.scroll_into_view_if_needed(timeout=min(timeout_ms, 5000))
    except Exception:
        pass
    locator.wait_for(state="visible", timeout=timeout_ms)
    if check:
        locator.check()
    else:
        locator.uncheck()


def _preferred_matches_target(hint: dict, target) -> bool:
    """Check that a preferred selector doesn't contradict the AI target.

    Rejects preferred selectors that target a clearly different field
    (e.g. preferred says label=Email but AI target says label=Password).
    """
    params = hint.get("params", {})
    hint_label = (params.get("label") or "").lower()
    hint_name = (params.get("name") or "").lower()

    target_label = (target.label or "").lower() if target.label else ""
    target_name = (target.name or "").lower() if target.name else ""

    # If both specify a label and they differ, reject
    if hint_label and target_label and hint_label != target_label:
        return False
    # If both specify a name and they differ, reject
    if hint_name and target_name and hint_name != target_name:
        return False
    return True


def _parse_selector_key(selector_key: str) -> dict | None:
    """Parse a 'strategy:params' key from InRunSelectorMemory into a resolver hint.

    The selector_key format matches what resolver.py stores:
    e.g. 'role:{"role": "button", "name": "Submit"}'
    """
    try:
        colon_idx = selector_key.index(":")
        strategy_str = selector_key[:colon_idx]
        params_str = selector_key[colon_idx + 1:]
        params = _json.loads(params_str) if params_str.startswith("{") else {"selector": params_str}
        return {
            "strategy": strategy_str,
            "params": params,
            "success_rate": 1.0,
        }
    except (ValueError, _json.JSONDecodeError):
        return None
