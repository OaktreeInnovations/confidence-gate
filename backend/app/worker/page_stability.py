"""Deterministic page-stability waits for Playwright.

Replaces arbitrary `wait_for_timeout()` calls with signal-based detection:
- DOM mutation quiescence (MutationObserver)
- Network idle (fetch/XHR tracking without hanging on WebSockets)
- CSS animation completion (Web Animations API)

All functions are sync (Playwright sync API) and non-fatal — they log
warnings on failure but never raise. This prevents stability infrastructure
from breaking test execution.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from playwright.sync_api import Page

logger = structlog.get_logger(__name__)

# Default timeouts — generous but bounded
_DOM_STABLE_TIMEOUT_MS = 5000
_DOM_SETTLE_MS = 300  # No mutations for this long = stable
_NETWORK_IDLE_TIMEOUT_MS = 5000
_NETWORK_SETTLE_MS = 500  # No in-flight requests for this long = idle
_ANIMATION_TIMEOUT_MS = 3000
_COMPOSITE_TIMEOUT_MS = 8000


def wait_for_dom_stable(
    page: Page,
    timeout_ms: int = _DOM_STABLE_TIMEOUT_MS,
    settle_ms: int = _DOM_SETTLE_MS,
) -> int:
    """Wait until DOM mutations stop for `settle_ms` milliseconds.

    Uses MutationObserver to detect when the page finishes updating.
    Returns the actual wait time in ms.
    """
    t0 = time.monotonic()
    try:
        page.evaluate(
            """([timeoutMs, settleMs]) => new Promise((resolve) => {
                let timer = null;
                let settled = false;
                const observer = new MutationObserver(() => {
                    if (timer) clearTimeout(timer);
                    timer = setTimeout(() => {
                        settled = true;
                        observer.disconnect();
                        resolve('stable');
                    }, settleMs);
                });
                observer.observe(document.body || document.documentElement, {
                    childList: true,
                    subtree: true,
                    attributes: true,
                    characterData: true,
                });
                // Start the settle timer immediately (handles already-stable pages)
                timer = setTimeout(() => {
                    if (!settled) {
                        observer.disconnect();
                        resolve('stable');
                    }
                }, settleMs);
                // Hard timeout
                setTimeout(() => {
                    if (!settled) {
                        observer.disconnect();
                        resolve('timeout');
                    }
                }, timeoutMs);
            })""",
            [timeout_ms, settle_ms],
        )
    except Exception as e:
        logger.debug("stability.dom_wait_failed", error=str(e)[:200])

    return int((time.monotonic() - t0) * 1000)


def wait_for_network_idle(
    page: Page,
    timeout_ms: int = _NETWORK_IDLE_TIMEOUT_MS,
    settle_ms: int = _NETWORK_SETTLE_MS,
    max_inflight: int = 0,
) -> int:
    """Wait until in-flight fetch/XHR requests drop to `max_inflight`.

    Unlike Playwright's built-in `networkidle`, this:
    - Only tracks fetch() and XMLHttpRequest (ignores WebSocket, EventSource)
    - Has a hard timeout to prevent infinite hangs
    - Resolves when inflight count stays <= max_inflight for settle_ms

    Returns the actual wait time in ms.
    """
    t0 = time.monotonic()
    try:
        page.evaluate(
            """([timeoutMs, settleMs, maxInflight]) => new Promise((resolve) => {
                let inflight = 0;
                let timer = null;
                let done = false;

                function checkIdle() {
                    if (done) return;
                    if (timer) clearTimeout(timer);
                    if (inflight <= maxInflight) {
                        timer = setTimeout(() => {
                            if (!done && inflight <= maxInflight) {
                                done = true;
                                resolve('idle');
                            }
                        }, settleMs);
                    }
                }

                // Monkey-patch fetch
                const origFetch = window.fetch;
                window.fetch = function(...args) {
                    inflight++;
                    return origFetch.apply(this, args).finally(() => {
                        inflight--;
                        checkIdle();
                    });
                };

                // Monkey-patch XMLHttpRequest
                const origOpen = XMLHttpRequest.prototype.open;
                const origSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(...args) {
                    this._tracked = true;
                    return origOpen.apply(this, args);
                };
                XMLHttpRequest.prototype.send = function(...args) {
                    if (this._tracked) {
                        inflight++;
                        this.addEventListener('loadend', () => {
                            inflight--;
                            checkIdle();
                        }, { once: true });
                    }
                    return origSend.apply(this, args);
                };

                // Start initial check (handles already-idle pages)
                checkIdle();

                // Hard timeout
                setTimeout(() => {
                    if (!done) {
                        done = true;
                        // Restore originals
                        window.fetch = origFetch;
                        XMLHttpRequest.prototype.open = origOpen;
                        XMLHttpRequest.prototype.send = origSend;
                        resolve('timeout');
                    }
                }, timeoutMs);
            })""",
            [timeout_ms, settle_ms, max_inflight],
        )
    except Exception as e:
        logger.debug("stability.network_wait_failed", error=str(e)[:200])

    return int((time.monotonic() - t0) * 1000)


def wait_for_animations_done(
    page: Page,
    timeout_ms: int = _ANIMATION_TIMEOUT_MS,
) -> int:
    """Wait until all CSS animations and transitions finish.

    Uses the Web Animations API (document.getAnimations()) which covers
    both CSS transitions and keyframe animations.

    Returns the actual wait time in ms.
    """
    t0 = time.monotonic()
    try:
        page.evaluate(
            """(timeoutMs) => new Promise((resolve) => {
                const animations = document.getAnimations();
                const running = animations.filter(
                    a => a.playState === 'running' || a.playState === 'pending'
                );
                if (running.length === 0) {
                    resolve('done');
                    return;
                }
                // Wait for all running animations to finish
                const allDone = Promise.all(
                    running.map(a => a.finished.catch(() => {}))
                );
                let resolved = false;
                allDone.then(() => {
                    if (!resolved) {
                        resolved = true;
                        resolve('done');
                    }
                });
                // Hard timeout
                setTimeout(() => {
                    if (!resolved) {
                        resolved = true;
                        resolve('timeout');
                    }
                }, timeoutMs);
            })""",
            timeout_ms,
        )
    except Exception as e:
        logger.debug("stability.animation_wait_failed", error=str(e)[:200])

    return int((time.monotonic() - t0) * 1000)


def wait_for_page_ready(
    page: Page,
    timeout_ms: int = _COMPOSITE_TIMEOUT_MS,
) -> dict[str, int]:
    """Composite wait: load state + DOM stable + network idle + animations done.

    First waits for the page load event (catches ongoing navigations),
    then runs DOM/network/animation checks sequentially.

    Returns a dict of phase timings in ms for telemetry:
        {"load_ms": N, "dom_ms": N, "network_ms": N, "animation_ms": N, "total_ms": N}
    """
    t0 = time.monotonic()

    # First: wait for any ongoing navigation to complete
    load_ms = 0
    try:
        t_load = time.monotonic()
        page.wait_for_load_state("load", timeout=min(timeout_ms, 5000))
        load_ms = int((time.monotonic() - t_load) * 1000)
    except Exception:
        load_ms = int((time.monotonic() - t0) * 1000)

    # Budget remaining time across phases
    elapsed = int((time.monotonic() - t0) * 1000)
    remaining = max(timeout_ms - elapsed, 1000)

    dom_budget = min(remaining // 2, _DOM_STABLE_TIMEOUT_MS)
    network_budget = min(remaining // 3, _NETWORK_IDLE_TIMEOUT_MS)
    animation_budget = min(remaining // 4, _ANIMATION_TIMEOUT_MS)

    dom_ms = wait_for_dom_stable(page, timeout_ms=dom_budget)
    network_ms = wait_for_network_idle(page, timeout_ms=network_budget)
    animation_ms = wait_for_animations_done(page, timeout_ms=animation_budget)

    total_ms = int((time.monotonic() - t0) * 1000)

    logger.debug(
        "stability.page_ready",
        load_ms=load_ms,
        dom_ms=dom_ms,
        network_ms=network_ms,
        animation_ms=animation_ms,
        total_ms=total_ms,
    )

    return {
        "load_ms": load_ms,
        "dom_ms": dom_ms,
        "network_ms": network_ms,
        "animation_ms": animation_ms,
        "total_ms": total_ms,
    }
