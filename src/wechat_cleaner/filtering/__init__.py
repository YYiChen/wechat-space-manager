"""Precise, side-effect-free filtering and selection for Phase 3."""

from .engine import (
    EligibilityDecision,
    ExclusionCode,
    FilterError,
    FilterExclusion,
    FilterResult,
    RetentionPolicy,
    apply_filter,
    build_selection_snapshot,
    compute_filter_digest,
    create_selection_snapshot,
    evaluate_eligibility,
    filter_digest,
    filter_records,
    paginate_records,
)

__all__ = [
    "EligibilityDecision",
    "ExclusionCode",
    "FilterError",
    "FilterExclusion",
    "FilterResult",
    "RetentionPolicy",
    "apply_filter",
    "build_selection_snapshot",
    "compute_filter_digest",
    "create_selection_snapshot",
    "evaluate_eligibility",
    "filter_digest",
    "filter_records",
    "paginate_records",
]
