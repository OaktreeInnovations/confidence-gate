"""Scorer-based selector resolver — main entry point.

Replaces the old first-match approach. Flow:
1. Build resolution chain from TargetDescriptor (same priority order)
2. For each strategy, build locator and collect ALL matching candidates
3. Score every candidate on visibility, enabled, match confidence, proximity
4. If top candidate is unambiguous (clear score gap) → return it
5. If ambiguous → run disambiguation layers (combination, proximity, AI)
6. Return ResolvedTarget with full scoring metadata for telemetry

Backward compatible: callers get (locator, strategy) plus optional metadata.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from app.worker.intent_schema import TargetDescriptor
from app.worker.selector_engine.disambiguation import (
    AMBIGUITY_THRESHOLD,
    disambiguate,
    is_ambiguous,
)
from app.worker.selector_engine.scoring import (
    CandidateScore,
    ScoringWeights,
    score_candidates,
)
from app.worker.selector_resilience import (
    SelectorStrategy,
    _build_locator,
)

if TYPE_CHECKING:
    from app.worker.ai_provider import AIProvider
    from app.worker.in_run_memory import InRunSelectorMemory
    from playwright.sync_api import Locator, Page

logger = structlog.get_logger(__name__)


@dataclass
class ResolvedTarget:
    """Result of selector resolution with full metadata."""

    locator: Locator
    strategy: SelectorStrategy
    score: CandidateScore
    resolution_ms: int = 0
    disambiguation_used: bool = False
    disambiguation_layer: str = ""
    strategies_tried: int = 0
    total_candidates_evaluated: int = 0
    selector_path: str = ""

    def to_telemetry(self) -> dict:
        """Serialize for telemetry recording."""
        return {
            "strategy": self.strategy.value,
            "params": self.score.params,
            "composite_score": round(self.score.composite, 3),
            "score_breakdown": {
                "strategy": round(self.score.strategy_score, 3),
                "visibility": round(self.score.visibility_score, 3),
                "enabled": round(self.score.enabled_score, 3),
                "match_confidence": round(self.score.match_confidence_score, 3),
                "proximity": round(self.score.proximity_score, 3),
            },
            "resolution_ms": self.resolution_ms,
            "disambiguation_used": self.disambiguation_used,
            "disambiguation_layer": self.disambiguation_layer,
            "strategies_tried": self.strategies_tried,
            "total_candidates_evaluated": self.total_candidates_evaluated,
            "selector_path": self.selector_path,
            "resolution_tier": self.score.resolution_tier,
        }


def resolve_target(
    page: Page,
    target: TargetDescriptor,
    timeout_ms: int = 5000,
    known_good_selectors: list[dict] | None = None,
    last_action_selector: str | None = None,
    ai_client: AIProvider | None = None,
    action_description: str = "",
    weights: ScoringWeights | None = None,
    in_run_memory: InRunSelectorMemory | None = None,
    action_index: int = 0,
    used_selector_paths: frozenset[str] | None = None,
) -> ResolvedTarget:
    """Resolve a TargetDescriptor to a Playwright Locator using scored selection.

    1. Builds strategy chain from target fields (+ known-good at front)
    2. For each strategy: builds locator, scores ALL candidates
    3. Picks the highest-scored candidate across all strategies
    4. If ambiguous: runs disambiguation layers
    5. Returns ResolvedTarget with locator + full metadata

    Args:
        page: Playwright Page instance.
        target: Semantic element descriptor from AI intent.
        timeout_ms: Total time budget for resolution.
        known_good_selectors: Previously successful selectors to try first.
        last_action_selector: CSS path of last interacted element (for proximity).
        ai_client: OpenAI client for AI disambiguation (optional, last resort).
        action_description: Human-readable action for disambiguation context.
        weights: Scoring weight overrides.
        in_run_memory: Per-run selector memory for blacklisting/promotion.
        action_index: Index of this action within the step (for memory keying).

    Returns:
        ResolvedTarget with the best locator and scoring metadata.

    Raises:
        RuntimeError: If no candidate passes minimum score threshold.
    """
    t0 = time.monotonic()
    tiers = _build_tiered_chains(target, known_good_selectors)

    if not tiers:
        raise RuntimeError(
            f"No selector strategies available for target: "
            f"{target.model_dump(exclude_none=True)}"
        )

    # Build history_records lookup for scoring: strategy:params → success_count
    history_records = _build_history_records(known_good_selectors)

    # Scope into iframe or shadow DOM if specified
    scoped_page = _scope_to_context(page, target)

    # Extract non-None fields from target for cross-attribute coherence checking
    target_fields = {
        k: v for k, v in {
            "label": target.label,
            "placeholder": target.placeholder,
            "name": target.name,
            "text": target.text,
            "role": target.role,
        }.items() if v
    }

    # Budget per strategy: split evenly but floor at 1.5s
    total_strategies = sum(len(t) for t in tiers)
    per_strategy_ms = max(timeout_ms // max(total_strategies, 1), 1500)

    all_candidates: list[CandidateScore] = []
    strategies_tried = 0

    for tier_idx, tier_chain in enumerate(tiers):
        tier_candidates: list[CandidateScore] = []
        early_exit = False

        for strategy, params in tier_chain:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            if elapsed_ms >= timeout_ms:
                break

            strategies_tried += 1
            remaining_ms = min(per_strategy_ms, timeout_ms - elapsed_ms)

            locator = _build_locator(scoped_page, strategy, params)
            if locator is None:
                continue

            candidates = score_candidates(
                page=scoped_page,
                locator=locator,
                strategy=strategy,
                params=params,
                weights=weights,
                last_action_selector=last_action_selector,
                timeout_ms=remaining_ms,
                history_records=history_records,
                action_hint=action_description,
                used_selector_paths=used_selector_paths,
                target_fields=target_fields,
            )

            # Tag candidates with resolution tier
            for c in candidates:
                c.resolution_tier = tier_idx + 1

            tier_candidates.extend(candidates)
            all_candidates.extend(candidates)

            # Early exit: high-confidence unique match in any tier
            if (
                candidates
                and candidates[0].composite >= 0.85
                and candidates[0].total_candidates == 1
            ):
                logger.debug(
                    "resolver.early_exit",
                    strategy=strategy.value,
                    score=round(candidates[0].composite, 3),
                    tier=tier_idx + 1,
                )
                early_exit = True
                break

        if early_exit:
            break

        # Check tier exit threshold
        if tier_candidates:
            tier_candidates.sort(key=lambda c: c.composite, reverse=True)
            threshold = _TIER_THRESHOLDS[min(tier_idx, len(_TIER_THRESHOLDS) - 1)]
            best = tier_candidates[0]

            if tier_idx == 0:
                # Tier 1: require unique + high score to stop searching
                if best.composite >= threshold and best.total_candidates == 1:
                    logger.debug(
                        "resolver.tier_exit",
                        tier=1,
                        score=round(best.composite, 3),
                    )
                    break
            else:
                # Tier 2+: meet threshold to stop searching
                if best.composite >= threshold:
                    logger.debug(
                        "resolver.tier_exit",
                        tier=tier_idx + 1,
                        score=round(best.composite, 3),
                    )
                    break

        # Log tier escalation
        logger.info(
            "resolver.tier_escalation",
            from_tier=tier_idx + 1,
            best_score=round(
                tier_candidates[0].composite, 3,
            ) if tier_candidates else 0.0,
        )

    # Tier 4: AI structural arbitration (only if tiers 1-3 found nothing viable)
    if ai_client and (
        not all_candidates
        or max(c.composite for c in all_candidates) < 0.5
    ):
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        remaining_budget = timeout_ms - elapsed_ms
        if remaining_budget > 1000:
            ai_candidate = _ai_structural_arbitration(
                page=scoped_page,
                target=target,
                action_description=action_description,
                ai_client=ai_client,
                weights=weights,
                last_action_selector=last_action_selector,
                timeout_ms=min(remaining_budget, 5000),
                history_records=history_records,
                used_selector_paths=used_selector_paths,
                target_fields=target_fields,
            )
            if ai_candidate:
                strategies_tried += 1
                all_candidates.append(ai_candidate)

    if not all_candidates:
        resolution_ms = int((time.monotonic() - t0) * 1000)
        strategies_list = [
            s.value for tier in tiers for s, _ in tier
        ]
        raise RuntimeError(
            f"All selector strategies found no candidates for target: "
            f"{target.model_dump(exclude_none=True)}. "
            f"Tried: {strategies_list} in {resolution_ms}ms"
        )

    # Sort all candidates across all strategies by composite score
    all_candidates.sort(key=lambda c: c.composite, reverse=True)

    # Filter out blacklisted selectors from in-run memory
    if in_run_memory:
        pre_filter_count = len(all_candidates)
        all_candidates = [
            c for c in all_candidates
            if not in_run_memory.is_blacklisted(
                action_index, f"{c.strategy.value}:{c.params}",
            )
        ]
        filtered = pre_filter_count - len(all_candidates)
        if filtered:
            logger.info(
                "resolver.blacklist_filtered",
                action_index=action_index,
                filtered=filtered,
                remaining=len(all_candidates),
            )

    if not all_candidates:
        resolution_ms = int((time.monotonic() - t0) * 1000)
        raise RuntimeError(
            f"All candidates blacklisted for target: "
            f"{target.model_dump(exclude_none=True)} in {resolution_ms}ms"
        )

    # Filter: only keep visible, enabled candidates above minimum threshold
    MIN_SCORE = 0.15
    viable = [c for c in all_candidates if c.composite >= MIN_SCORE]
    if not viable:
        # Fall back to best of what we have
        viable = all_candidates[:1]

    # Confidence threshold: if best candidate is too weak, attempt AI escalation
    MIN_SELECTOR_CONFIDENCE = 0.45
    if (
        viable[0].composite < MIN_SELECTOR_CONFIDENCE
        and ai_client
        and viable[0].resolution_tier < 4  # Don't re-escalate if AI already tried
    ):
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        remaining_budget = timeout_ms - elapsed_ms
        if remaining_budget > 1000:
            logger.info(
                "resolver.confidence_escalation",
                best_score=round(viable[0].composite, 3),
                threshold=MIN_SELECTOR_CONFIDENCE,
            )
            ai_candidate = _ai_structural_arbitration(
                page=scoped_page,
                target=target,
                action_description=action_description,
                ai_client=ai_client,
                weights=weights,
                last_action_selector=last_action_selector,
                timeout_ms=min(remaining_budget, 5000),
                history_records=history_records,
                used_selector_paths=used_selector_paths,
                target_fields=target_fields,
            )
            if ai_candidate and ai_candidate.composite >= MIN_SELECTOR_CONFIDENCE:
                viable.insert(0, ai_candidate)
                viable.sort(key=lambda c: c.composite, reverse=True)

    winner = viable[0]
    disambiguation_used = False
    disambiguation_layer = ""

    # Check for ambiguity among viable candidates
    if len(viable) >= 2 and is_ambiguous(viable):
        logger.info(
            "resolver.ambiguous",
            top_scores=[round(c.composite, 3) for c in viable[:5]],
            target=target.model_dump(exclude_none=True),
        )

        disambiguated = disambiguate(
            page=page,
            candidates=viable[:10],  # Cap disambiguation candidates
            action_description=action_description,
            ai_client=ai_client,
            last_action_selector=last_action_selector,
        )

        if disambiguated is not None:
            winner = disambiguated
            disambiguation_used = True
            disambiguation_layer = _detect_disambiguation_layer(
                viable, disambiguated,
            )

    resolution_ms = int((time.monotonic() - t0) * 1000)

    # Extract selector path for future proximity scoring
    selector_path = ""
    try:
        selector_path = winner.locator.evaluate("""(el) => {
            const parts = [];
            let node = el;
            for (let d = 0; d < 5 && node && node !== document.body; d++) {
                let s = node.tagName.toLowerCase();
                if (node.id) {
                    s += '#' + node.id;
                } else {
                    const nm = node.getAttribute('name');
                    const tp = node.getAttribute('type');
                    if (nm) s += '[name="' + nm + '"]';
                    else if (tp && node.tagName === 'INPUT') s += '[type="' + tp + '"]';
                    const parent = node.parentElement;
                    if (parent) {
                        const sibs = Array.from(parent.children).filter(c => c.tagName === node.tagName);
                        if (sibs.length > 1) s += ':nth-of-type(' + (sibs.indexOf(node) + 1) + ')';
                    }
                }
                parts.unshift(s);
                node = node.parentElement;
            }
            return parts.join(' > ');
        }""")
    except Exception:
        pass

    result = ResolvedTarget(
        locator=winner.locator,
        strategy=winner.strategy,
        score=winner,
        resolution_ms=resolution_ms,
        disambiguation_used=disambiguation_used,
        disambiguation_layer=disambiguation_layer,
        strategies_tried=strategies_tried,
        total_candidates_evaluated=len(all_candidates),
        selector_path=selector_path,
    )

    logger.info(
        "resolver.resolved",
        strategy=winner.strategy.value,
        composite=round(winner.composite, 3),
        candidates_evaluated=len(all_candidates),
        disambiguation=disambiguation_used,
        resolution_ms=resolution_ms,
    )

    return result


# Tier exit thresholds: Tier 1 = 0.85 (strict), Tier 2 = 0.70, Tier 3 = 0.50
_TIER_THRESHOLDS = [0.85, 0.70, 0.50]


def _build_tiered_chains(
    target: TargetDescriptor,
    known_good: list[dict] | None = None,
) -> list[list[tuple[SelectorStrategy, dict]]]:
    """Build tiered strategy chains from TargetDescriptor fields.

    Tier 1 — Strict: known_good, test_id, role+name, label, placeholder
    Tier 2 — Structural: interactive_ancestor, aria_widget, near_label
    Tier 3 — Proximity: text, css, role-only relaxation, semantic expansions

    Returns list of tiers, each tier a list of (strategy, params) pairs.
    Empty tiers are omitted.
    """
    tier1: list[tuple[SelectorStrategy, dict]] = []
    tier2: list[tuple[SelectorStrategy, dict]] = []
    tier3: list[tuple[SelectorStrategy, dict]] = []

    # Track what we've added to avoid duplicates across tiers
    seen: set[tuple[SelectorStrategy, tuple]] = set()

    def _add(tier: list, strategy: SelectorStrategy, params: dict) -> None:
        key = (strategy, tuple(sorted(params.items())))
        if key not in seen:
            seen.add(key)
            tier.append((strategy, params))

    # --- Tier 1: Strict semantic match ---

    # Known-good selectors at the front, ordered by success rate
    if known_good:
        sorted_good = sorted(
            known_good,
            key=lambda e: e.get("success_rate", 0.0),
            reverse=True,
        )
        for entry in sorted_good:
            try:
                strategy = SelectorStrategy(entry.get("strategy", ""))
                params = entry.get("params", {})
                if params:
                    _add(tier1, strategy, params)
            except (ValueError, KeyError):
                continue

    # data-testid (most stable)
    if target.test_id:
        _add(tier1, SelectorStrategy.TEST_ID, {"test_id": target.test_id})

    # role + name (semantic)
    if target.role:
        role_params: dict = {"role": target.role}
        # Prefer name; fall back to text so intents using "text" instead of
        # "name" still get a scoped ROLE locator (e.g. get_by_role("button", name="Log In"))
        role_name = target.name or target.text
        if role_name:
            role_params["name"] = role_name
        _add(tier1, SelectorStrategy.ROLE, role_params)

    # label association (form fields)
    if target.label:
        _add(tier1, SelectorStrategy.LABEL, {"label": target.label})

    # placeholder text (input hints)
    if target.placeholder:
        _add(tier1, SelectorStrategy.PLACEHOLDER, {"placeholder": target.placeholder})

    # --- Tier 2: Structural discovery ---

    search_text = target.name or target.text or target.label or ""
    if search_text:
        _add(tier2, SelectorStrategy.INTERACTIVE_ANCESTOR, {"text": search_text})
        _add(tier2, SelectorStrategy.ARIA_WIDGET, {"text": search_text})
        _add(tier2, SelectorStrategy.NEAR_LABEL, {"label": search_text})

    # --- Tier 3: Proximity / text match + relaxations ---

    # Visible text content
    if target.text:
        _add(tier3, SelectorStrategy.TEXT, {"text": target.text, "exact": True})

    # CSS fallback
    if target.css:
        _add(tier3, SelectorStrategy.CSS, {"selector": target.css})

    # Relaxations from role+name
    if target.role and target.name:
        _add(tier3, SelectorStrategy.ROLE, {"role": target.role})  # drop name
        _add(tier3, SelectorStrategy.TEXT, {"text": target.name, "exact": False})
        # Semantic name expansion (e.g. "Today" → [aria-current='date'])
        for css_sel in _expand_semantic_name(target.name):
            _add(tier3, SelectorStrategy.CSS, {"selector": css_sel})

    return [t for t in [tier1, tier2, tier3] if t]


def _ai_structural_arbitration(
    page: Page,
    target: TargetDescriptor,
    action_description: str,
    ai_client: AIProvider,
    weights: ScoringWeights | None = None,
    last_action_selector: str | None = None,
    timeout_ms: int = 5000,
    history_records: dict[str, int] | None = None,
    used_selector_paths: frozenset[str] | None = None,
    target_fields: dict[str, str] | None = None,
) -> CandidateScore | None:
    """Tier 4: Ask AI to propose a CSS selector when tiers 1-3 fail.

    Captures accessible tree + simplified DOM excerpt, sends to AI.
    If AI proposes a selector with confidence >= 0.6, scores through pipeline.
    Returns None if AI is unavailable or confidence too low.
    """
    import json

    raw = ai_client.raw_client
    if raw is None:
        return None

    try:
        # Capture accessible tree (compact representation)
        try:
            a11y_snapshot = page.accessibility.snapshot(interesting_only=True)
            a11y_text = json.dumps(a11y_snapshot, indent=None, default=str)[:2000]
        except Exception:
            a11y_text = "(unavailable)"

        # Capture simplified DOM excerpt via JS
        try:
            dom_excerpt = page.evaluate("""() => {
                const els = [];
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_ELEMENT,
                    { acceptNode: (n) => {
                        const r = n.getBoundingClientRect();
                        return (r.width > 0 && r.height > 0)
                            ? NodeFilter.FILTER_ACCEPT
                            : NodeFilter.FILTER_REJECT;
                    }}
                );
                let count = 0;
                while (walker.nextNode() && count < 150) {
                    const el = walker.currentNode;
                    const tag = el.tagName.toLowerCase();
                    const attrs = [];
                    for (const a of ['id','class','role','aria-label','data-testid',
                                     'name','type','placeholder','href','for',
                                     'aria-haspopup','aria-expanded']) {
                        const v = el.getAttribute(a);
                        if (v) attrs.push(a + '="' + v.substring(0, 50) + '"');
                    }
                    const text = (el.textContent || '').trim().substring(0, 30);
                    els.push('<' + tag + (attrs.length ? ' ' + attrs.join(' ') : '')
                             + '>' + text + '</' + tag + '>');
                    count++;
                }
                return els.join('\\n');
            }""")
        except Exception:
            dom_excerpt = "(unavailable)"

        target_desc = target.model_dump(exclude_none=True)
        prompt = (
            "You are a DOM element finder. Given a target element description and "
            "page DOM, propose a CSS selector to locate the element.\n\n"
            f"Target: {json.dumps(target_desc)}\n"
            f"Action context: {action_description}\n\n"
            f"Accessibility tree:\n{a11y_text}\n\n"
            f"DOM excerpt:\n{dom_excerpt[:3000]}\n\n"
            "Return ONLY a JSON object with two fields:\n"
            '{"selector": "your-css-selector", "confidence": 0.0-1.0}\n'
            "confidence should reflect how certain you are this selector uniquely "
            "identifies the correct element."
        )

        response = raw.chat.completions.create(
            model="gpt-5-mini",
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=200,
        )

        text = response.choices[0].message.content.strip()
        # Parse JSON from response (handle markdown code blocks)
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        result = json.loads(text)
        selector = result.get("selector", "")
        confidence = result.get("confidence", 0.0)

        if not selector or confidence < 0.6:
            logger.debug(
                "resolver.ai_arbitration_low_confidence",
                selector=selector,
                confidence=confidence,
            )
            return None

        locator = page.locator(selector)
        candidates = score_candidates(
            page=page,
            locator=locator,
            strategy=SelectorStrategy.CSS,
            params={"selector": selector, "ai_proposed": True},
            weights=weights,
            last_action_selector=last_action_selector,
            timeout_ms=min(timeout_ms, 3000),
            history_records=history_records,
            action_hint=action_description,
            used_selector_paths=used_selector_paths,
            target_fields=target_fields,
        )

        if candidates:
            candidates[0].resolution_tier = 4
            logger.info(
                "resolver.ai_arbitration_success",
                selector=selector,
                confidence=confidence,
                score=round(candidates[0].composite, 3),
            )
            return candidates[0]

        return None

    except Exception as e:
        logger.warning(
            "resolver.ai_arbitration_error",
            error=str(e)[:200],
        )
        return None


def _build_history_records(
    known_good: list[dict] | None,
) -> dict[str, int] | None:
    """Build a history_records lookup from known_good selectors.

    Maps ``"strategy:params"`` → ``success_count`` for use in scoring's
    history bonus calculation.

    Returns None if no usable history data exists.
    """
    if not known_good:
        return None

    records: dict[str, int] = {}
    for entry in known_good:
        strategy = entry.get("strategy", "")
        params = entry.get("params", {})
        success_count = entry.get("success_count", 0)
        if strategy and params and success_count > 0:
            key = f"{strategy}:{params}"
            records[key] = success_count

    return records or None


def _expand_semantic_name(name: str) -> list[str]:
    """Return CSS selectors for well-known semantic names.

    Delegates to the semantic plugin registry which combines core ARIA
    selectors with framework-specific selectors for detected frameworks.
    """
    from app.worker.selector_engine.semantic_plugins import get_semantic_expansions

    return get_semantic_expansions(name)


def _detect_disambiguation_layer(
    original_viable: list[CandidateScore],
    winner: CandidateScore,
) -> str:
    """Detect which disambiguation layer resolved the ambiguity."""
    # Check if the winner moved from a non-top position
    if original_viable and original_viable[0] is not winner:
        # Winner was reranked — likely strategy_combination or proximity
        if winner.proximity_score > original_viable[0].proximity_score:
            return "proximity"
        return "strategy_combination"
    return "ai"


def _scope_to_context(page: Page, target: TargetDescriptor) -> Page:
    """Scope into iframe or shadow DOM if target specifies it.

    Returns a FrameLocator-like object that supports the same get_by_* API,
    or the original page if no scoping needed.

    For shadow DOM: Uses Playwright's >> (shadow piercing) CSS selector
    to scope within the shadow host.

    For iframes: Uses page.frame_locator() to scope into the iframe.
    Tries by name, then by URL pattern, then by CSS selector.
    """
    # Iframe scoping
    if target.iframe:
        frame_ref = target.iframe
        try:
            # Try by frame name
            frame = page.frame(name=frame_ref)
            if frame is not None:
                logger.debug("resolver.scoped_iframe", method="name", ref=frame_ref)
                return frame
        except Exception:
            pass

        try:
            # Try by URL pattern
            frame = page.frame(url=frame_ref)
            if frame is not None:
                logger.debug("resolver.scoped_iframe", method="url", ref=frame_ref)
                return frame
        except Exception:
            pass

        try:
            # Try by CSS selector → frame_locator
            fl = page.frame_locator(frame_ref)
            # frame_locator returns a FrameLocator which supports get_by_* methods
            logger.debug("resolver.scoped_iframe", method="css", ref=frame_ref)
            return fl
        except Exception:
            logger.warning("resolver.iframe_scope_failed", ref=frame_ref)

    # Shadow DOM scoping: Playwright auto-pierces shadow DOM by default,
    # but if a shadow_host is specified, we scope the locator within it
    # using CSS >> shadow piercing syntax
    if target.shadow_host:
        # Playwright handles shadow DOM automatically with get_by_* locators.
        # For CSS selectors, we'll use the >> syntax in _build_tiered_chains.
        # No special scoping needed here — mark it for the chain builder.
        logger.debug("resolver.shadow_host_noted", host=target.shadow_host)

    return page
