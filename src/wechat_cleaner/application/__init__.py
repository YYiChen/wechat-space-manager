"""Headless application workflow facades for desktop and automation clients.

Two facades live here and they have deliberately different boundaries:

* :class:`~wechat_cleaner.application.real_workflow.RealReadOnlySession` — the
  **read-only** real-WeChat session used by the Beta desktop shell.  It never
  imports cleanup, planning or executor code.
* :class:`~wechat_cleaner.application.workflow.ApplicationFacade` — the
  synthetic-path workflow that *does* compose cleanup planning.

Importing this package must not force cleanup code into a read-only caller, so
the workflow facade is resolved lazily via :pep:`562` module ``__getattr__``.
That keeps the historical ``from wechat_cleaner.application import
ApplicationFacade`` API working while letting a packaged read-only Beta exclude
``wechat_cleaner.cleanup`` entirely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .real_workflow import SESSION_VERSION, RealReadOnlySession, SessionManifest

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from .workflow import (
        ApplicationFacade,
        ApplicationWorkflow,
        CancellationToken,
        ConfirmationResult,
        DatabaseImport,
        ProgressCallback,
        ProgressEvent,
        RecoveryResult,
        ScanRegistration,
        WorkflowCancelled,
        WorkflowError,
        WorkflowFacade,
    )

__all__ = [
    "ApplicationFacade",
    "ApplicationWorkflow",
    "CancellationToken",
    "ConfirmationResult",
    "DatabaseImport",
    "ProgressEvent",
    "ProgressCallback",
    "RealReadOnlySession",
    "RecoveryResult",
    "SESSION_VERSION",
    "ScanRegistration",
    "SessionManifest",
    "WorkflowCancelled",
    "WorkflowError",
    "WorkflowFacade",
]

# Names resolved from the workflow module on first access.
_WORKFLOW_EXPORTS = frozenset(__all__) - {
    "RealReadOnlySession",
    "SESSION_VERSION",
    "SessionManifest",
}


def __getattr__(name: str) -> Any:
    """Resolve workflow-facade symbols on first use (PEP 562)."""
    if name in _WORKFLOW_EXPORTS:
        from . import workflow as _workflow  # noqa: PLC0415 - intentional lazy import

        return getattr(_workflow, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
