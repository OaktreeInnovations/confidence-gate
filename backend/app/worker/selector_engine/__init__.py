"""Selector Resolution Engine — scorer-based candidate selection.

Replaces the old first-match approach with:
1. Collect ALL candidates per strategy
2. Score each on visibility, enabled state, match confidence, DOM proximity
3. Disambiguate when top candidates have close scores
4. Return the best candidate with full scoring metadata

Public API:
    resolve_target(page, target, ...) -> ResolvedTarget
    describe_resolution(result) -> str
"""

from app.worker.selector_engine.resolver import (
    ResolvedTarget,
    resolve_target,
)
from app.worker.selector_engine.scoring import (
    CandidateScore,
    ScoringWeights,
)

__all__ = [
    "CandidateScore",
    "ResolvedTarget",
    "ScoringWeights",
    "resolve_target",
]
