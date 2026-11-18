"""Entry point dispatching between the interactive TUI and the CLI."""

import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 0 or "--tui" in args:
        from .tui import run_tui
        run_tui()
        return 0
    from .cli import run_cli
    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
