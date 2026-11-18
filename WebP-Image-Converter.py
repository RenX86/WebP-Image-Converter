import os
import sys
import shutil
import argparse
import json
from PIL import Image
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import List, Set, Dict, Optional, Tuple, Any, Union

try:
    from tqdm import tqdm
except ImportError:
    print("Warning: tqdm not found. Install it with 'pip install tqdm' for progress bars.")
    # Fallback class for when tqdm is not installed
    class tqdm:
        def __init__(self, iterable: Optional[Any] = None, total: Optional[int] = None, desc: str = "", **kwargs: Any):
            self.iterable = iterable
            self.total = total
            self.desc = desc
            self.count = 0

        def __iter__(self):
            if self.iterable is not None:
                for item in self.iterable:
                    yield item
                    self.update()
            print()  # New line at the end

        def update(self, n: int = 1) -> None:
            self.count += n
            if self.total:
                pct = int(100 * self.count / self.total)
                print(f"\r{self.desc}: {pct:3d}%|  {self.count}/{self.total}", end='', flush=True)
            else:
                print(f"\r{self.desc}: {self.count}", end='', flush=True)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            print() # Ensure newline on exit

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IS_WINDOWS = sys.platform.startswith('win')

# Exit codes (used by app.py to report the real outcome)
EXIT_OK = 0
EXIT_PARTIAL_FAILURE = 1   # at least one file failed or was skipped because of a conflict
EXIT_FATAL = 2             # bad arguments, missing tools, invalid folder

# Default constants
DEFAULT_CONFIG_FILE = "config.json"
DEFAULT_WEBP_QUALITY = 100
DEFAULT_DELETE_ORIGINALS = False   # Safe default: originals are kept unless explicitly requested
DEFAULT_LOSSLESS = False
DEFAULT_OVERWRITE = False
DEFAULT_SUPPORTED_FORMATS = ['.jpg', '.jpeg', '.png', '.tif', '.tiff', '.bmp', '.gif']
DEFAULT_MAX_WORKERS = os.cpu_count() or 1
DEFAULT_TOOL_PATHS = {
    "cwebp": "cwebp",
    "exiftool": "exiftool",
    "gif2webp": "gif2webp",   # optional, needed to keep GIF animations
}
REQUIRED_TOOLS = ['cwebp', 'exiftool']
OPTIONAL_TOOLS = ['gif2webp']


@dataclass
class Settings:
    """Runtime settings, resolved from config.json + command-line arguments."""
    quality: int = DEFAULT_WEBP_QUALITY
    delete_originals: bool = DEFAULT_DELETE_ORIGINALS
    lossless: bool = DEFAULT_LOSSLESS
    overwrite: bool = DEFAULT_OVERWRITE
    max_workers: int = DEFAULT_MAX_WORKERS
    supported_formats: Set[str] = field(default_factory=lambda: set(DEFAULT_SUPPORTED_FORMATS))
    # Resolved executable paths. Optional tools that are unavailable are absent.
    tools: Dict[str, str] = field(default_factory=dict)


def resolve_config_path(config_file: str) -> str:
    """Absolute paths are used as-is; relative paths are tried against the CWD, then the script directory."""
    if os.path.isabs(config_file):
        return config_file
    if os.path.exists(config_file):
        return os.path.abspath(config_file)
    return os.path.join(SCRIPT_DIR, config_file)


def load_config(config_file: str = DEFAULT_CONFIG_FILE) -> Dict[str, Any]:
    """Load configuration from a JSON file."""
    config_path = resolve_config_path(config_file)

    config: Dict[str, Any] = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            logging.info(f"Configuration loaded from {config_path}")
        except json.JSONDecodeError as e:
            logging.error(f"Invalid JSON in config file {config_path}: {e}")
        except Exception as e:
            logging.error(f"Error reading config file {config_path}: {e}")
    elif config_file == DEFAULT_CONFIG_FILE:
        # Create default config file if it doesn't exist
        default_config = {
            "webp_quality": DEFAULT_WEBP_QUALITY,
            "delete_originals": DEFAULT_DELETE_ORIGINALS,
            "lossless": DEFAULT_LOSSLESS,
            "overwrite": DEFAULT_OVERWRITE,
            "supported_formats": DEFAULT_SUPPORTED_FORMATS,
            "max_workers": DEFAULT_MAX_WORKERS,
            "tool_paths": DEFAULT_TOOL_PATHS
        }
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(default_config, f, indent=4)
            logging.info(f"Default configuration file created: {config_path}")
        except Exception as e:
            logging.error(f"Could not create default config file: {e}")
    else:
        logging.warning(f"Config file not found: {config_path} (using defaults)")

    return config


def resolve_tool(tool_key: str, configured: str) -> Optional[str]:
    """
    Resolve a tool to an executable path.
    Relative paths containing a directory (e.g. "bin/cwebp.exe") are resolved against the script directory.
    Windows-only binaries (*.exe) are ignored on other platforms so the system tool from PATH is used instead.
    """
    candidates: List[str] = []
    if configured:
        if os.path.isabs(configured) or os.path.dirname(configured):
            path = configured if os.path.isabs(configured) else os.path.join(SCRIPT_DIR, configured)
            if not IS_WINDOWS and path.lower().endswith('.exe'):
                logging.debug(f"Ignoring Windows binary for {tool_key} on this platform: {path}")
            else:
                candidates.append(path)
                if IS_WINDOWS and not path.lower().endswith('.exe'):
                    candidates.append(path + '.exe')
        else:
            candidates.append(configured)
    candidates.append(tool_key)  # Fall back to the plain command name on PATH

    for candidate in candidates:
        if os.path.dirname(candidate):
            if os.path.isfile(candidate):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None


def check_external_tools(tool_paths: Dict[str, str]) -> Optional[Dict[str, str]]:
    """Check if required external tools are available. Returns resolved paths, or None if any required tool is missing."""
    resolved: Dict[str, str] = {}
    missing_tools: List[str] = []

    for tool_key in REQUIRED_TOOLS + OPTIONAL_TOOLS:
        path = resolve_tool(tool_key, tool_paths.get(tool_key, tool_key))
        if path:
            resolved[tool_key] = path
            logging.debug(f"Using {tool_key}: {path}")
        elif tool_key in REQUIRED_TOOLS:
            missing_tools.append(tool_key)

    if missing_tools:
        logging.error(f"Missing required tools: {', '.join(missing_tools)}")
        logging.error("Please install the required tools before running this script.")
        logging.error("For cwebp: Install WebP tools from https://developers.google.com/speed/webp/download")
        logging.error("For exiftool: Install ExifTool from https://exiftool.org/")
        logging.error("Or update config.json with the absolute paths to these tools.")
        return None

    if 'gif2webp' not in resolved:
        logging.info("gif2webp not found: animated GIFs will be skipped (static GIFs use cwebp).")

    logging.info("All required tools are available.")
    return resolved


def setup_logging(log_file: Optional[str] = None, verbose: bool = False) -> None:
    """Setup logging with optional file output."""
    # Clear any existing handlers
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)

    # Set logging level
    level = logging.DEBUG if verbose else logging.INFO

    # Create formatter
    console_formatter = logging.Formatter('%(levelname)s - %(message)s')
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(level)

    # Add console handler
    logging.getLogger().addHandler(console_handler)
    logging.getLogger().setLevel(logging.DEBUG if log_file else level)

    # File handler if specified
    if log_file:
        file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.DEBUG)  # Log everything to file
        logging.getLogger().addHandler(file_handler)


def safe_decode(byte_string: Union[bytes, str, None]) -> str:
    """Safely decode a byte string, falling back to a simple representation if it fails."""
    if byte_string is None:
        return ""
    if isinstance(byte_string, str):
        return byte_string
    try:
        return byte_string.decode('utf-8')
    except UnicodeDecodeError:
        return byte_string.decode('utf-8', errors='replace')


def run_subprocess(command: List[str]) -> Tuple[str, str]:
    """Run a subprocess command and return decoded (stdout, stderr). Raises CalledProcessError on failure."""
    result = subprocess.run(command, capture_output=True, check=True)
    return safe_decode(result.stdout), safe_decode(result.stderr)


def run_exiftool(exiftool: str, args: List[str], tag_values: Optional[Dict[str, str]] = None) -> Tuple[str, str]:
    """
    Run exiftool using an arguments file to avoid command line encoding issues on Windows.

    args: plain arguments (one per line in the args file, so they must not contain newlines).
    tag_values: tag -> value. Each value is written to its own UTF-8 temp file and passed as
                "-TAG<=file", so multi-line values (A1111 parameters, ComfyUI JSON) survive intact.
                Values written inline in an args file would be split at every newline.
    """
    temp_files: List[str] = []
    try:
        file_args: List[str] = []
        for tag, value in (tag_values or {}).items():
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.txt', delete=False) as vf:
                vf.write(value.encode('utf-8'))
                temp_files.append(vf.name)
            file_args.append(f"-{tag}<={vf.name}")

        # Create a temporary file to hold the arguments, one per line
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', suffix='.args', delete=False) as f:
            # Ensure we use -charset filename=UTF8 inside the args file too
            f.write("-charset\nfilename=UTF8\n")
            for arg in file_args + args:
                if '\n' in arg or '\r' in arg:
                    raise ValueError(f"exiftool argument contains a newline: {arg[:60]!r}")
                f.write(f"{arg}\n")
            temp_files.append(f.name)
            args_file = f.name

        # Construct command: exiftool -@ args_file
        return run_subprocess([exiftool, '-@', args_file])
    finally:
        # Cleanup temp files
        for path in temp_files:
            try:
                os.unlink(path)
            except OSError:
                pass


def to_exif_datetime(value: str) -> Optional[str]:
    """Convert a PNG creation_time (RFC 1123 or ISO 8601, usually) to EXIF 'YYYY:MM:DD HH:MM:SS'."""
    value = value.strip()
    if not value:
        return None
    parsers = (
        lambda v: datetime.fromisoformat(v.replace('Z', '+00:00')),
        parsedate_to_datetime,
        lambda v: datetime.strptime(v, '%Y:%m:%d %H:%M:%S'),
    )
    for parse in parsers:
        try:
            return parse(value).strftime('%Y:%m:%d %H:%M:%S')
        except (ValueError, TypeError, IndexError):
            continue
    return None


def is_animated(path: str) -> bool:
    try:
        with Image.open(path) as im:
            return bool(getattr(im, 'is_animated', False))
    except Exception:
        return False


def plan_conversions(image_files: List[str], overwrite: bool = False) -> Tuple[List[Tuple[str, str]], List[str]]:
    """
    Decide the output path for every input so that no two inputs write the same file and existing
    WebP files are never clobbered (unless overwrite=True).

    - "photo.jpg" alone                  -> "photo.webp"
    - "photo.jpg" + "photo.png"          -> "photo_jpg.webp" + "photo_png.webp"
    - "photo.jpg" with existing photo.webp -> "photo_jpg.webp" (or overwrite when overwrite=True)
    Inputs whose candidate names are all taken are returned in the skipped list.
    """
    def key(path: str) -> str:
        # normcase makes the comparison case-insensitive on Windows, matching the filesystem
        return os.path.normcase(os.path.abspath(path))

    groups: Dict[str, List[str]] = {}
    for src in image_files:
        groups.setdefault(key(os.path.splitext(src)[0] + '.webp'), []).append(src)

    claimed: Set[str] = set()
    plan: List[Tuple[str, str]] = []
    skipped: List[str] = []
    needs_alt: List[str] = []

    # Pass 1: unambiguous inputs get the plain "<stem>.webp" name
    for src in image_files:
        primary = os.path.splitext(src)[0] + '.webp'
        if len(groups[key(primary)]) == 1 and (overwrite or not os.path.exists(primary)):
            claimed.add(key(primary))
            plan.append((src, primary))
        else:
            needs_alt.append(src)

    # Pass 2: conflicting inputs get "<stem>_<ext>.webp"
    for src in needs_alt:
        stem, ext = os.path.splitext(src)
        alt = f"{stem}_{ext.lstrip('.').lower()}.webp"
        if key(alt) in claimed or (os.path.exists(alt) and not overwrite):
            logging.warning(f"Skipping {src}: output name conflict ({os.path.basename(alt)} already exists)")
            skipped.append(src)
            continue
        claimed.add(key(alt))
        plan.append((src, alt))
        logging.info(f"Name conflict: {os.path.basename(src)} -> {os.path.basename(alt)}")

    return plan, skipped


def encode(settings: Settings, src: str, dst: str) -> bool:
    """Encode src into dst. Returns True on success; removes partial output on failure."""
    ext = os.path.splitext(src)[1].lower()
    if ext == '.gif' and is_animated(src):
        if 'gif2webp' not in settings.tools:
            logging.error(f"Skipping animated GIF (gif2webp not available, animation would be lost): {src}")
            return False
        cmd = [settings.tools['gif2webp'], '-mt', '-metadata', 'all']
        if not settings.lossless:
            cmd += ['-lossy', '-q', str(settings.quality)]
        cmd += [src, '-o', dst]
    else:
        cmd = [settings.tools['cwebp'], '-mt', '-metadata', 'all']
        if settings.lossless:
            cmd += ['-lossless', '-exact']
        else:
            cmd += ['-q', str(settings.quality)]
        cmd += [src, '-o', dst]

    try:
        stdout, stderr = run_subprocess(cmd)
        logging.debug(f"encoder stdout: {stdout}")
        logging.debug(f"encoder stderr: {stderr}")
    except (subprocess.CalledProcessError, OSError) as e:
        detail = safe_decode(getattr(e, 'stderr', None)) or str(e)
        logging.error(f"Failed to convert {src}: {detail.strip()}")
        try:
            if os.path.exists(dst):
                os.remove(dst)  # plan_conversions guarantees we don't remove a pre-existing user file
        except OSError:
            pass
        return False

    if not os.path.exists(dst) or os.path.getsize(dst) == 0:
        logging.error(f"Encoder produced no output for {src}")
        return False
    return True


def collect_metadata(src: str) -> Tuple[List[str], Dict[str, str]]:
    """
    Build exiftool (plain args, tag values) that copy src's metadata to the WebP output.
    Returns ([], {}) when there is nothing to copy.
    """
    ext = os.path.splitext(src)[1].lower()
    args: List[str] = []
    tags: Dict[str, str] = {}

    if ext in ('.jpg', '.jpeg', '.tif', '.tiff'):
        # Copy all EXIF/XMP/IPTC data
        args += ['-TagsFromFile', src, '-all:all']

    elif ext == '.png':
        # EXIF/XMP/ICC in PNG are already copied by "cwebp -metadata all".
        # Copy the PNG text chunks used by Stable Diffusion UIs.
        info: Dict[str, Any] = {}
        try:
            with Image.open(src) as im:
                info = dict(im.info)
        except Exception as img_err:
            logging.warning(f"Could not read metadata from {src}: {img_err}")

        def text(k: str) -> str:
            v = info.get(k, "")
            return v if isinstance(v, str) else ""

        # A1111 / Forge / Fooocus style: read back from EXIF UserComment by those UIs
        if text("parameters"):
            tags['UserComment'] = text("parameters")
        # ComfyUI style: ComfyUI stores WebP workflows as "workflow:<json>" in EXIF Make
        # and "prompt:<json>" in EXIF Model, so drag-and-drop restores the workflow
        if text("workflow"):
            tags['Make'] = "workflow:" + text("workflow")
        if text("prompt"):
            tags['Model'] = "prompt:" + text("prompt")
        if text("software"):
            tags['Software'] = text("software")
        if text("creation_time"):
            exif_dt = to_exif_datetime(text("creation_time"))
            if exif_dt:
                args.append(f'-DateTimeOriginal={exif_dt}')
            else:
                logging.debug(f"Unrecognised creation_time in {src}: {text('creation_time')!r}")

    elif ext in ('.bmp', '.gif'):
        # Copy available metadata from BMP/GIF
        try:
            with Image.open(src) as im:
                metadata = dict(im.info)
        except Exception as img_err:
            logging.warning(f"Could not read metadata from {src}: {img_err}")
            metadata = {}
        for k, value in metadata.items():
            if isinstance(value, bytes):
                value = safe_decode(value)
            if not isinstance(value, str) or not value:
                continue
            # Map common keys to standard EXIF tags
            key_lower = str(k).lower()
            if key_lower in ('comment', 'description', 'parameters'):
                tags['UserComment'] = value
            elif key_lower in ('author', 'artist'):
                tags['Artist'] = value
            elif key_lower in ('title', 'subject'):
                tags['XMP-dc:Title'] = value
            elif key_lower in ('date', 'datetime', 'timestamp'):
                exif_dt = to_exif_datetime(value)
                if exif_dt:
                    args.append(f'-DateTimeOriginal={exif_dt}')

    return args, tags


def process_image_file(settings: Settings, src: str, dst: str) -> bool:
    """
    Convert one image to WebP and copy its metadata.
    The original is only deleted when encoding AND metadata copying both succeeded.
    Returns True on success.
    """
    try:
        logging.info(f"Processing: {src}")

        if not encode(settings, src, dst):
            return False

        metadata_ok = True
        args, tags = collect_metadata(src)
        if args or tags:
            try:
                stdout, stderr = run_exiftool(settings.tools['exiftool'], ['-overwrite_original'] + args + [dst], tags)
                logging.debug(f"exiftool stdout: {stdout}")
                if stderr.strip():
                    logging.debug(f"exiftool stderr: {stderr}")
                # exiftool can exit 0 even when a file couldn't be opened, so also inspect stderr
                errors = [ln for ln in stderr.splitlines() if ln.strip().lower().startswith(('error', 'invalid filename'))]
                if errors:
                    metadata_ok = False
                    logging.error(f"Failed to write metadata for {src}: {' | '.join(errors)}")
            except subprocess.CalledProcessError as e:
                metadata_ok = False
                logging.error(f"Failed to write metadata for {src}: {safe_decode(e.stderr).strip()}")
            except (OSError, ValueError) as e:
                metadata_ok = False
                logging.error(f"Failed to write metadata for {src}: {e}")

        # Cleanup ExifTool backup files if they exist
        backup_file = f"{dst}_original"
        if os.path.exists(backup_file):
            try:
                os.remove(backup_file)
                logging.debug(f"Deleted ExifTool backup: {backup_file}")
            except OSError as e:
                logging.warning(f"Could not delete backup file {backup_file}: {e}")

        if not metadata_ok:
            logging.error(f"Original kept because metadata could not be copied: {src}")
            return False

        if settings.delete_originals:
            try:
                os.remove(src)
                logging.info(f"Deleted original file: {src}")
            except OSError as e:
                logging.error(f"Error deleting original file {src}: {e}")
                return False

        logging.info(f"Successfully processed: {src} -> {os.path.basename(dst)}")
        return True

    except Exception as e:
        logging.error(f"Unexpected error processing {src}: {e}")
        return False


def validate_folder_path(path: str) -> Optional[str]:
    """Validate the folder path and return normalized path or None if invalid."""
    if not path:
        logging.error("No folder path provided.")
        return None

    # Clean the path
    path = path.strip().strip("\"'")

    # Normalize the path
    path = os.path.normpath(path)

    # Check if path exists
    if not os.path.exists(path):
        logging.error(f"Folder does not exist: {path}")
        return None

    # Check if path is a directory
    if not os.path.isdir(path):
        logging.error(f"Path is not a directory: {path}")
        return None

    # Check if path is readable
    if not os.access(path, os.R_OK):
        logging.error(f"Folder is not readable: {path}")
        return None

    return path


def scan_folder(folder_path: str, formats: Set[str]) -> List[str]:
    image_files: List[str] = []
    for root, _, files in os.walk(folder_path):
        for file in sorted(files):
            if any(file.lower().endswith(ext) for ext in formats):
                file_path = os.path.join(root, file)
                # Validate individual file
                if os.path.isfile(file_path) and os.access(file_path, os.R_OK):
                    image_files.append(file_path)
                else:
                    logging.warning(f"Skipping inaccessible file: {file_path}")
    return image_files


def quality_type(value: str) -> int:
    try:
        q = int(float(value))
    except ValueError:
        raise argparse.ArgumentTypeError(f"quality must be a number, got {value!r}")
    if not 0 <= q <= 100:
        raise argparse.ArgumentTypeError("quality must be between 0 and 100")
    return q


def positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}")
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def build_settings(args: argparse.Namespace, config: Dict[str, Any]) -> Settings:
    """Merge config file values with command-line overrides (CLI wins)."""
    def pick(cli_value: Any, config_key: str, default: Any) -> Any:
        return cli_value if cli_value is not None else config.get(config_key, default)

    formats = pick(args.formats, "supported_formats", DEFAULT_SUPPORTED_FORMATS)
    formats = {(f if f.startswith('.') else '.' + f).lower() for f in formats}

    return Settings(
        quality=quality_type(str(pick(args.quality, "webp_quality", DEFAULT_WEBP_QUALITY))),
        delete_originals=bool(pick(args.delete_originals, "delete_originals", DEFAULT_DELETE_ORIGINALS)),
        lossless=bool(pick(args.lossless, "lossless", DEFAULT_LOSSLESS)),
        overwrite=bool(pick(args.overwrite, "overwrite", DEFAULT_OVERWRITE)),
        max_workers=positive_int(str(pick(args.workers, "max_workers", DEFAULT_MAX_WORKERS))),
        supported_formats=formats,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert images to WebP format")
    parser.add_argument("folder_path", nargs='?', help="Folder path containing images to convert")
    parser.add_argument("--quality", "-q", type=quality_type, default=None, help="WebP quality (0-100, default: from config)")
    parser.add_argument("--lossless", action="store_true", default=None, help="Use lossless WebP encoding (quality is ignored)")
    parser.add_argument("--lossy", action="store_false", dest="lossless", help="Use lossy encoding (overrides config)")
    parser.add_argument("--keep-originals", action="store_false", dest="delete_originals", default=None, help="Keep original files after conversion (default)")
    parser.add_argument("--delete-originals", action="store_true", dest="delete_originals", help="Delete original files after a fully successful conversion")
    parser.add_argument("--overwrite", action="store_true", default=None, help="Overwrite existing .webp files instead of choosing a new name")
    parser.add_argument("--workers", "-w", type=positive_int, default=None, help="Number of worker threads (default: from config)")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be converted without making changes")
    parser.add_argument("--formats", nargs='+', default=None, help="Supported image formats (default: from config)")
    parser.add_argument("--log-file", type=str, help="Log file path to write logs")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--config", type=str, default=DEFAULT_CONFIG_FILE, help="Configuration file path (default: config.json next to the script)")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    # Setup logging
    setup_logging(log_file=args.log_file, verbose=args.verbose)

    # Load configuration from file (after parsing, so --config is honoured)
    config = load_config(args.config)

    try:
        settings = build_settings(args, config)
    except argparse.ArgumentTypeError as e:
        logging.error(f"Invalid configuration value: {e}")
        return EXIT_FATAL

    # Check if external tools are available
    tool_paths = dict(DEFAULT_TOOL_PATHS)
    tool_paths.update(config.get("tool_paths", {}) or {})
    tools = check_external_tools(tool_paths)
    if tools is None:
        return EXIT_FATAL
    settings.tools = tools

    # Log startup info
    mode = "lossless" if settings.lossless else f"quality={settings.quality}"
    logging.info(f"Starting WebP conversion with {mode}, workers={settings.max_workers}")
    if settings.delete_originals:
        logging.info("Original files will be deleted after a fully successful conversion")
    else:
        logging.info("Original files will be kept after conversion")

    # Get folder path from argument or input
    folder_path = args.folder_path if args.folder_path else input("Enter the folder path containing images: ")

    # Validate folder path
    validated_path = validate_folder_path(folder_path)
    if not validated_path:
        return EXIT_FATAL
    folder_path = validated_path

    if args.dry_run:
        logging.info("DRY RUN MODE: No files will be modified.")

    try:
        image_files = scan_folder(folder_path, settings.supported_formats)
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

    failures = 0
    # Use tqdm for progress bar
    with tqdm(total=len(plan), desc="Converting images") as pbar:
        with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
            # Submit all tasks
            future_to_file = {executor.submit(process_image_file, settings, src, dst): src for src, dst in plan}

            # Process completed tasks
            for future in as_completed(future_to_file):
                file_path = future_to_file[future]
                try:
                    if not future.result():
                        failures += 1
                except Exception as e:
                    failures += 1
                    logging.error(f"Error processing file {file_path}: {e}")
                pbar.update(1)  # Update progress bar

    succeeded = len(plan) - failures
    logging.info(f"All files processed! {succeeded} succeeded, {failures} failed, {len(skipped)} skipped.")
    return EXIT_PARTIAL_FAILURE if (failures or skipped) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
