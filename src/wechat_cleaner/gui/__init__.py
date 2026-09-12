"""Optional PySide6 desktop entry point.

Importing :mod:`wechat_cleaner.gui` remains safe in a headless environment;
the dependency is loaded only when the desktop launcher is called.

Boundary note
-------------
The read-only Beta shell (:mod:`.real_session`, :mod:`.real_window`,
:mod:`.launcher`) must not drag in the synthetic-path workflow facade, which
composes cleanup planning.  Only the read-only symbols are imported eagerly; the
:mod:`.services` facade is resolved lazily via :pep:`562` so a packaged
read-only build can exclude ``wechat_cleaner.cleanup``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .real_session import (
    READ_ONLY_NOTICE,
    AccountOption,
    CacheStatus,
    FacadeError,
    FakeRealSessionFacade,
    PreviewOutcome,
    RealReadOnlyPort,
    RealSessionFacade,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime import
    from .services import (
        ApplicationFacadeAdapter,
        ApplicationFacadePort,
        ConfirmationResult,
        DesktopFacadeConfig,
        FakeApplicationFacade,
        RecoveryResult,
    )

__all__ = [
    "AccountOption",
    "ApplicationFacadePort",
    "ApplicationFacadeAdapter",
    "CacheStatus",
    "ConfirmationResult",
    "DesktopFacadeConfig",
    "FacadeError",
    "FakeApplicationFacade",
    "FakeRealSessionFacade",
    "PreviewOutcome",
    "READ_ONLY_NOTICE",
    "RealReadOnlyPort",
    "RealSessionFacade",
    "RecoveryResult",
    "WorkflowFacadeError",
    "create_application_facade_from_args",
    "create_default_application_facade",
    "default_filter_spec",
    "main",
]

# Symbols that live in the synthetic-path facade module.
_SERVICES_EXPORTS = frozenset(
    {
        "ApplicationFacadeAdapter",
        "ApplicationFacadePort",
        "ConfirmationResult",
        "DesktopFacadeConfig",
        "FakeApplicationFacade",
        "RecoveryResult",
        "create_application_facade_from_args",
        "create_default_application_facade",
        "default_filter_spec",
        "WorkflowFacadeError",
    }
)


def __getattr__(name: str) -> Any:
    """Resolve synthetic-path facade symbols on first use (PEP 562)."""
    if name in _SERVICES_EXPORTS:
        from . import services as _services  # noqa: PLC0415 - intentional lazy import

        if name == "WorkflowFacadeError":
            return _services.FacadeError
        return getattr(_services, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


def main(argv: list[str] | None = None) -> int:
    try:
        from .window import run
    except ModuleNotFoundError as exc:
        if exc.name == "PySide6" or (exc.name and exc.name.startswith("PySide6.")):
            print("桌面 GUI 依赖未安装，请运行 `pip install -e .[gui]`。")
            return 2
        raise
    return run(argv)
