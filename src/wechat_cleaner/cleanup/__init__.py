"""Safety-gated cleanup planning for local WeChat storage.

This package deliberately stops before mutation.  It produces immutable
``CleanupPlan`` objects, re-checks a plan against a user-owned copy immediately
before an external executor acts, and normalizes executor results into a
``CleanupReceipt``.  No function in this package deletes, moves, or rewrites a
file.
"""

from .safety import (
    CleanupSafetyError,
    ExecutionOutcome,
    PlanBuildResult,
    PreflightReport,
    PreflightStatus,
    TargetPreflight,
    build_plan,
    build_receipt,
    preflight,
)

__all__ = [
    "CleanupSafetyError",
    "ExecutionOutcome",
    "PlanBuildResult",
    "PreflightReport",
    "PreflightStatus",
    "TargetPreflight",
    "build_plan",
    "build_receipt",
    "preflight",
]
