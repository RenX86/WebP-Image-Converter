"""Entry point: launches the interactive TUI by default; --cli runs CLI mode."""

import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if "-h" in args or "--help" in args:
        from .cli import build_parser
        build_parser().print_help()
        return 0

    if "--cli" in args:
        from .cli import run_cli
        return run_cli([a for a in args if a != "--cli"])

    # Default: launch TUI directly (optionally pre-selecting a positional folder path)
    tui_args = [a for a in args if a != "--tui"]
    if len(tui_args) == 0:
        initial_folder = None
    elif len(tui_args) == 1 and not tui_args[0].startswith("-"):
        initial_folder = tui_args[0]
    else:
        print(
            "Opening TUI ignores CLI flags unless --cli is specified.\n"
            "Run 'webp-converter --cli ...' for command-line mode, or 'webp-converter --help'.",
            file=sys.stderr,
        )
        return 2

    from .tui import run_tui
    run_tui(initial_folder=initial_folder)
    return 0


if __name__ == "__main__":
    sys.exit(main())
