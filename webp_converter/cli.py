"""Command-line interface for webp-converter."""

import argparse
import logging
import os
from typing import Any, Dict, List, Optional

from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .config import (
    DEFAULT_CONFIG_FILE,
    Settings,
    load_config,
    normalise_formats,
    settings_from_config,
)
from .core import BatchEvent, plan_conversions, run_batch, scan_folder, validate_folder_path

EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1
EXIT_FATAL = 2


def setup_logging(log_file: Optional[str] = None, verbose: bool = False) -> None:
    """Configure console and optional file logging."""
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    level = logging.DEBUG if verbose else logging.INFO
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("%(levelname)s - %(message)s"))
    console_handler.setLevel(level)
    logging.getLogger().addHandler(console_handler)
    logging.getLogger().setLevel(logging.DEBUG if log_file else level)

    if log_file:
        file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        file_handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(file_handler)


def quality_type(value: str) -> int:
    try:
        q = int(float(value))
    except ValueError:
        raise argparse.ArgumentTypeError(f"quality must be a number, got {value!r}")
    if not 0 <= q <= 100:
        raise argparse.ArgumentTypeError("quality must be between 0 and 100")
    return q


def method_type(value: str) -> int:
    try:
        m = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"method must be an integer, got {value!r}")
    if not 0 <= m <= 6:
        raise argparse.ArgumentTypeError("method must be between 0 and 6")
    return m


def positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webp-converter",
        description="Convert images to WebP format. Run with no arguments to open the interactive TUI.",
    )
    parser.add_argument("folder_path", nargs="?", help="Folder path containing images to convert")
    parser.add_argument("--tui", action="store_true", help="Launch the interactive terminal UI")
    parser.add_argument("--quality", "-q", type=quality_type, default=None, help="WebP quality 0-100 (default: 90)")
    parser.add_argument("--lossless", action="store_true", default=None, help="Use lossless WebP encoding")
    parser.add_argument("--lossy", action="store_false", dest="lossless", help="Use lossy encoding (overrides config)")
    parser.add_argument("--method", "-m", type=method_type, default=None, help="Compression effort 0-6 (default: 4)")
    parser.add_argument("--keep-originals", action="store_false", dest="delete_originals", default=None, help="Keep original files (default)")
    parser.add_argument("--delete-originals", action="store_true", dest="delete_originals", help="Delete original files after successful conversion")
    parser.add_argument("--overwrite", action="store_true", default=None, help="Overwrite existing .webp files")
    parser.add_argument("--recursive", action="store_true", default=None, help="Scan subfolders recursively (default)")
    parser.add_argument("--no-recursive", action="store_false", dest="recursive", help="Only scan the top-level folder")
    parser.add_argument("--workers", "-w", type=positive_int, default=None, help="Number of worker threads")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be converted without making changes")
    parser.add_argument("--formats", nargs="+", default=None, help="Supported image formats (e.g. .jpg .png)")
    parser.add_argument("--log-file", type=str, help="Write detailed logs to a file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG_FILE, help="Configuration JSON path")
    return parser


def build_settings(args: argparse.Namespace, config: Dict[str, Any]) -> Settings:
    """Merge config.json values with CLI overrides (CLI wins)."""
    base = settings_from_config(config)
    return Settings(
        quality=args.quality if args.quality is not None else base.quality,
        lossless=args.lossless if args.lossless is not None else base.lossless,
        method=args.method if args.method is not None else base.method,
        delete_originals=args.delete_originals if args.delete_originals is not None else base.delete_originals,
        overwrite=args.overwrite if args.overwrite is not None else base.overwrite,
        recursive=args.recursive if args.recursive is not None else base.recursive,
        max_workers=args.workers if args.workers is not None else base.max_workers,
        supported_formats=normalise_formats(args.formats) if args.formats is not None else base.supported_formats,
    )


def format_bytes(num_bytes: int) -> str:
    size = float(abs(num_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def run_cli(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(log_file=args.log_file, verbose=args.verbose)

    config = load_config(args.config)
    try:
        settings = build_settings(args, config)
    except ValueError as e:
        logging.error(f"Invalid configuration value: {e}")
        return EXIT_FATAL

    mode = "lossless" if settings.lossless else f"quality={settings.quality}"
    logging.info(
        f"Starting WebP conversion ({mode}, method={settings.method}, workers={settings.max_workers})"
    )
    if settings.delete_originals:
        logging.info("Original files will be deleted after a successful conversion")
    else:
        logging.info("Original files will be kept after conversion")

    folder_input = args.folder_path if args.folder_path else input("Enter the folder path containing images: ")
    folder_path = validate_folder_path(folder_input)
    if not folder_path:
        return EXIT_FATAL

    if args.dry_run:
        logging.info("DRY RUN MODE: No files will be modified.")

    try:
        image_files = scan_folder(folder_path, settings.supported_formats, recursive=settings.recursive)
    except PermissionError:
        logging.error(f"Permission denied while accessing folder: {folder_path}")
        return EXIT_FATAL
    except Exception as e:
        logging.error(f"Error while scanning folder: {e}")
        return EXIT_FATAL

    if not image_files:
        logging.info("No supported image files found in the specified folder.")
        return EXIT_OK

    plan, skipped = plan_conversions(image_files, overwrite=settings.overwrite)
    logging.info(f"Found {len(image_files)} image files, {len(plan)} to process, {len(skipped)} skipped.")

    if args.dry_run:
        logging.info("Files to be processed:")
        for src, dst in plan:
            logging.info(f"  {src} -> {os.path.basename(dst)}")
        logging.info("Dry run completed. No files were modified.")
        return EXIT_OK

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        transient=True,
    ) as progress:
        task_id = progress.add_task("Converting images", total=len(plan))

        def on_event(ev: BatchEvent) -> None:
            if ev.kind in ("file_completed", "file_failed"):
                progress.advance(task_id, 1)

        summary = run_batch(settings, plan, skipped=skipped, on_event=on_event)

    saved = summary.saved_bytes
    pct = (100.0 * saved / summary.bytes_before) if summary.bytes_before else 0.0
    direction = "saved" if saved >= 0 else "larger"
    logging.info(
        f"Done: {summary.succeeded} succeeded, {summary.failed} failed, {summary.skipped} skipped "
        f"({format_bytes(abs(saved))} {direction}, {abs(pct):.1f}%)."
    )
    return EXIT_PARTIAL_FAILURE if (summary.failed or summary.skipped) else EXIT_OK
