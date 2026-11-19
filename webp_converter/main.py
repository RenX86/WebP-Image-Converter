"""Entry point: launches the interactive TUI by default; --cli runs CLI mode."""

import sys
from typing import List, Optional, Tuple


def _split_before_double_dash(args: List[str]) -> Tuple[List[str], List[str]]:
    if "--" in args:
        idx = args.index("--")
        return args[:idx], args[idx + 1 :]
    return args, []


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    opts, trailing = _split_before_double_dash(args)

    if "-h" in opts or "--help" in opts:
        from .cli import build_parser
        build_parser().print_help()
        return 0

    has_cli = "--cli" in opts
    has_tui = "--tui" in opts
    if has_cli and has_tui:
        print("Cannot combine --cli and --tui.", file=sys.stderr)
        return 2

    if has_cli:
        cli_opts = [a for a in opts if a != "--cli"]
        cli_args = cli_opts + (["--"] + trailing if "--" in args else [])
        from .cli import run_cli
        return run_cli(cli_args)

    # Default: launch TUI directly (supports optional --config <path> and positional folder path)
    config_file: Optional[str] = None
    positionals: List[str] = []
    i = 0
    while i < len(opts):
        tok = opts[i]
        if tok == "--tui":
            i += 1
            continue
        if tok == "--config":
            if i + 1 >= len(opts):
                print("--config requires a file path.", file=sys.stderr)
                return 2
            config_file = opts[i + 1]
            i += 2
            continue
        if tok.startswith("--config="):
            config_file = tok.split("=", 1)[1]
            i += 1
            continue
        if tok.startswith("-"):
            print(
                f"Unrecognized TUI option: {tok}\n"
                "Run 'webp-converter --cli ...' for command-line mode, or 'webp-converter --help'.",
                file=sys.stderr,
            )
            return 2
        positionals.append(tok)
        i += 1

    positionals.extend(trailing)
    if len(positionals) > 1:
        print(
            "Expected at most one folder path when launching the TUI.\n"
            "Run 'webp-converter --cli ...' for command-line mode, or 'webp-converter --help'.",
            file=sys.stderr,
        )
        return 2

    initial_folder = positionals[0] if positionals else None
    from .tui import run_tui
    run_tui(initial_folder=initial_folder, config_file=config_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
