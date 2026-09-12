"""Recoverable cleanup execution over immutable plans."""

from .executor import (
    ExecutionResult,
    ExecutorError,
    ExecutorErrorCode,
    RecoverableExecutor,
    RestoreResult,
)

__all__ = [
    "ExecutionResult",
    "ExecutorError",
    "ExecutorErrorCode",
    "RecoverableExecutor",
    "RestoreResult",
]
