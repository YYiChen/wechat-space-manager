"""Desktop-first entry point with an explicit read-only legacy CLI mode."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Sequence

_HELP = """usage: wechat-space-manager [--gui [GUI_ARGS...]]
       wechat-space-manager legacy <browse|preview|cache> [OPTIONS]
       wechat-space-manager --legacy-cli <browse|preview|cache> [OPTIONS]

With no arguments, launch the desktop GUI.  Use the explicit legacy mode for
the existing read-only CLI and automation-friendly JSON output.
"""


def _print_help() -> None:
    print(_HELP, end="")


def _legacy_cli(argv: Sequence[str]) -> int:
    from wechat_cleaner.ui.cli import main as legacy_main

    return int(legacy_main(list(argv)))


def _desktop_gui(argv: Sequence[str]) -> int:
    try:
        module = importlib.import_module("wechat_cleaner.gui")
    except ModuleNotFoundError as exc:
        missing = exc.name or "unknown dependency"
        if missing == "wechat_cleaner.gui" or missing.startswith("wechat_cleaner.gui."):
            print(
                "desktop GUI is not available in this checkout; "
                "complete P3-GUI-SHELL/P3-GUI-FLOW first, then run `pip install -e .[gui]`.",
                file=sys.stderr,
            )
        elif missing == "PySide6" or missing.startswith("PySide6."):
            print(
                "desktop GUI dependencies are missing; install them with `pip install -e .[gui]`.",
                file=sys.stderr,
            )
        else:
            print(f"desktop GUI dependency is missing: {missing}", file=sys.stderr)
        return 2
    launcher = getattr(module, "main", None)
    if not callable(launcher):
        print("desktop GUI module has no callable main(argv) entry point", file=sys.stderr)
        return 2
    result = launcher(list(argv))
    return 0 if result is None else int(result)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to the desktop GUI or an explicitly selected read-only CLI."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return _desktop_gui(())
    if args[0] in {"-h", "--help"}:
        _print_help()
        return 0
    if args[0] in {"legacy", "--legacy-cli"}:
        return _legacy_cli(args[1:])
    if args[0] == "--gui":
        return _desktop_gui(args[1:])
    print(f"unknown entrypoint option: {args[0]}", file=sys.stderr)
    _print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
