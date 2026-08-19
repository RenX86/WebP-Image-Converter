import os
import sys
import argparse
import json
from pathlib import Path
from PIL import Image
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import tempfile
from datetime import datetime
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
            if self.iterable:
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

# Default constants
DEFAULT_WEBP_QUALITY = "100"
DEFAULT_DELETE_ORIGINALS = True
DEFAULT_SUPPORTED_FORMATS = ['.jpg', '.jpeg', '.png', '.tiff', '.bmp', '.gif']
DEFAULT_MAX_WORKERS = os.cpu_count() or 1
DEFAULT_TOOL_PATHS = {
    "cwebp": "cwebp", 
    "exiftool": "exiftool"
}

# Global configurations (populated in main)
WEBP_QUALITY: str = DEFAULT_WEBP_QUALITY
DELETE_ORIGINALS: bool = DEFAULT_DELETE_ORIGINALS
MAX_WORKERS: int = DEFAULT_MAX_WORKERS
SUPPORTED_FORMATS: Set[str] = set(DEFAULT_SUPPORTED_FORMATS)
TOOL_PATHS: Dict[str, str] = DEFAULT_TOOL_PATHS.copy()

def load_config(config_file: str = "config.json") -> Dict[str, Any]:
    """Load configuration from a JSON file."""
    # Resolve config file path relative to the script's directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, config_file)

    config: Dict[str, Any] = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
            logging.info(f"Configuration loaded from {config_path}")
        except json.JSONDecodeError as e:
            logging.error(f"Invalid JSON in config file {config_path}: {e}")
        except Exception as e:
            logging.error(f"Error reading config file {config_path}: {e}")
    else:
        # Create default config file if it doesn't exist
        default_config = {
            "webp_quality": DEFAULT_WEBP_QUALITY,
            "delete_originals": DEFAULT_DELETE_ORIGINALS,
            "supported_formats": DEFAULT_SUPPORTED_FORMATS,
            "max_workers": DEFAULT_MAX_WORKERS,
            "tool_paths": DEFAULT_TOOL_PATHS
        }
        try:
            with open(config_path, 'w') as f:
                json.dump(default_config, f, indent=4)
            logging.info(f"Default configuration file created: {config_path}")
        except Exception as e:
            logging.error(f"Could not create default config file: {e}")

    return config

def check_external_tools() -> bool:
    """Check if required external tools are available."""
    required_tools = ['cwebp', 'exiftool']
    missing_tools: List[str] = []
    
    global TOOL_PATHS

    for tool_key in required_tools:
        # Get path from config or default to the command name
        tool_cmd = TOOL_PATHS.get(tool_key, tool_key)
        
        # If the user provided a specific path, check if it exists directly
        if os.path.isabs(tool_cmd) or os.path.dirname(tool_cmd):
            if os.path.exists(tool_cmd):
                # Tool found at specific path, no need to check PATH
                continue
            
            # If specific path not found, try adding .exe on Windows
            if sys.platform.startswith('win') and not tool_cmd.lower().endswith('.exe'):
                    if os.path.exists(tool_cmd + '.exe'):
                        TOOL_PATHS[tool_key] = tool_cmd + '.exe'
                        continue
            
            logging.error(f"Configured path for {tool_key} not found: {tool_cmd}")
            missing_tools.append(tool_key)
            continue
        
        # Check if tool is in PATH
        try:
            if sys.platform.startswith('win'):
                # On Windows, 'where' command
                result = subprocess.run(['where', tool_cmd], capture_output=True, text=True)
            else:
                # On Linux/Mac, 'which' command
                result = subprocess.run(['which', tool_cmd], capture_output=True, text=True)

            if result.returncode != 0:
                 missing_tools.append(tool_key)
        except Exception:
            missing_tools.append(tool_key)

    if missing_tools:
        logging.error(f"Missing required tools: {', '.join(missing_tools)}")
        logging.error("Please install the required tools before running this script.")
        logging.error("For cwebp: Install WebP tools from https://developers.google.com/speed/webp/download")
        logging.error("For exiftool: Install ExifTool from https://exiftool.org/")
        logging.error("Or update config.json with the absolute paths to these tools.")
        return False

    logging.info(f"All required tools are available.")
    return True

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
    logging.getLogger().setLevel(level)

    # File handler if specified
    if log_file:
        file_handler = logging.FileHandler(log_file, mode='a')
        file_handler.setFormatter(file_formatter)
        file_handler.setLevel(logging.DEBUG)  # Log everything to file
        logging.getLogger().addHandler(file_handler)

def safe_decode(byte_string: Union[bytes, str]) -> str:
    """Safely decode a byte string, falling back to a simple representation if it fails."""
    if isinstance(byte_string, str):
        return byte_string
    try:
        return byte_string.decode('utf-8')
    except UnicodeDecodeError:
        return str(byte_string)

def run_subprocess(command: List[str]) -> Tuple[str, str]:
    """Run a subprocess command and safely handle its output."""
    try:
        # Use full paths if available in TOOL_PATHS, otherwise assume command is in PATH
        cmd_executable = command[0]
        # Map generic tool names to configured paths
        if cmd_executable == 'cwebp':
             command[0] = TOOL_PATHS.get('cwebp', 'cwebp')
        elif cmd_executable == 'exiftool':
             command[0] = TOOL_PATHS.get('exiftool', 'exiftool')

        result = subprocess.run(command, capture_output=True, check=True)
        return safe_decode(result.stdout), safe_decode(result.stderr)
    except subprocess.CalledProcessError as e:
        # Don't log error here, let the caller handle it or log it with context
        # But we do need to return the error output for debugging
        raise e 

def run_exiftool_with_argsfile(args: List[str]) -> Tuple[str, str]:
    """
    Run exiftool using an arguments file to avoid command line encoding issues on Windows.
    args: List of arguments *excluding* the exiftool executable itself.
    """
    # Create a temporary file to hold the arguments
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as f:
        # Write arguments, one per line
        # Ensure we use -charset filename=UTF8 inside the args file too
        f.write("-charset\nfilename=UTF8\n")
        for arg in args:
            f.write(f"{arg}\n")
        args_file = f.name

    try:
        # Construct command: exiftool -@ args_file
        cmd = ['exiftool', '-@', args_file]
        return run_subprocess(cmd)
    finally:
        # Cleanup temp file
        try:
            os.unlink(args_file)
        except OSError:
            pass

def process_image_file(file_path: str) -> None:
    """
    Process an individual image file, converting it to WebP format.
    Optimized for memory usage by using context managers and proper resource cleanup.
    """
    try:
        if not any(file_path.lower().endswith(ext) for ext in SUPPORTED_FORMATS):
            logging.warning(f"Unsupported file format: {file_path}")
            return

        logging.info(f"Processing: {file_path}")

        filename = file_path
        filename_out = f'{os.path.splitext(filename)[0]}.webp'
        
        cwebp_cmd = ['cwebp', '-q', WEBP_QUALITY, filename, '-o', filename_out]
        
        try:
            stdout, stderr = run_subprocess(cwebp_cmd)
            logging.debug(f"cwebp stdout: {stdout}")
            logging.debug(f"cwebp stderr: {stderr}")
        except subprocess.CalledProcessError as e:
             logging.error(f"Failed to convert {filename}: {safe_decode(e.stderr)}")
             return

        # Handle metadata preservation based on file format
        file_extension = os.path.splitext(filename)[1].lower()
        
        # Base args for argfile (no need for 'exiftool' command here)
        exiftool_base_args = ['-overwrite_original']

        if file_extension == '.png':
            # Copy PNG Chunk data from original PNG
            # Using context manager to ensure proper resource cleanup
            user_comment = ""
            creation_time = ""
            software = ""
            comfyui_prompt = ""
            comfyui_workflow = ""
            
            try:
                with Image.open(filename) as im:
                    # Extract metadata without keeping the image in memory unnecessarily
                    # A1111-style metadata
                    user_comment = im.info.get("parameters", "")
                    creation_time = im.info.get("creation_time", "")
                    software = im.info.get("software", "")
                    # ComfyUI / Fooocus metadata
                    comfyui_prompt = im.info.get("prompt", "")
                    comfyui_workflow = im.info.get("workflow", "")
            except Exception as img_err:
                 logging.warning(f"Could not read metadata from {filename}: {img_err}")

            # Write EXIF to WEBP
            if user_comment or creation_time or software or comfyui_prompt or comfyui_workflow:
                # Build args list
                exiftool_args = exiftool_base_args.copy()

                # Combine all generation metadata into UserComment
                # A1111 parameters take priority, then ComfyUI prompt/workflow
                combined_comment_parts = []
                if user_comment:
                    combined_comment_parts.append(user_comment)
                if comfyui_prompt:
                    combined_comment_parts.append(f"[ComfyUI Prompt]\n{comfyui_prompt}")
                if comfyui_workflow:
                    combined_comment_parts.append(f"[ComfyUI Workflow]\n{comfyui_workflow}")
                
                if combined_comment_parts:
                    combined_comment = "\n\n".join(combined_comment_parts)
                    exiftool_args.append(f'-UserComment={combined_comment}')
                if creation_time:
                    exiftool_args.append(f'-DateTimeOriginal={creation_time}')
                if software:
                    exiftool_args.append(f'-Software={software}')

                exiftool_args.append(filename_out)

                try:
                    stdout, stderr = run_exiftool_with_argsfile(exiftool_args)
                    logging.debug(f"exiftool stdout: {stdout}")
                    logging.debug(f"exiftool stderr: {stderr}")
                except subprocess.CalledProcessError as e:
                     logging.warning(f"Failed to write metadata for {filename}: {safe_decode(e.stderr)}")
                     
        elif file_extension in ['.jpg', '.jpeg']:
            # Copy all EXIF data from JPEG
            try:
                # Use args file helper
                stdout, stderr = run_exiftool_with_argsfile(['-TagsFromFile', filename, '-all:all', filename_out])
                logging.debug(f"exiftool stdout: {stdout}")
                logging.debug(f"exiftool stderr: {stderr}")
            except subprocess.CalledProcessError as e:
                logging.warning(f"Failed to copy tags for {filename}: {safe_decode(e.stderr)}")
                
        elif file_extension in ['.tiff']:
            # Copy all metadata from TIFF
            try:
                stdout, stderr = run_exiftool_with_argsfile(['-TagsFromFile', filename, '-all:all', filename_out])
                logging.debug(f"exiftool stdout: {stdout}")
                logging.debug(f"exiftool stderr: {stderr}")
            except subprocess.CalledProcessError as e:
                 logging.warning(f"Failed to copy tags for {filename}: {safe_decode(e.stderr)}")
                 
        elif file_extension in ['.bmp', '.gif']:
            # Copy available metadata from BMP/GIF
            metadata = {}
            try:
                with Image.open(filename) as im:
                    # Extract available metadata
                    metadata = getattr(im, 'tag_v2', {}) if hasattr(im, 'tag_v2') else im.info
            except Exception as img_err:
                logging.warning(f"Could not read metadata from {filename}: {img_err}")

            # Write metadata to WEBP
            if metadata:
                # For BMP/GIF, try to extract relevant metadata
                exiftool_args = exiftool_base_args.copy()

                for key, value in metadata.items():
                    if isinstance(value, str) and len(value) > 0:
                        # Map common keys to standard EXIF tags
                        key_lower = str(key).lower()
                        if key_lower in ['comment', 'description', 'parameters']:
                            exiftool_args.append(f'-UserComment={value}')
                        elif key_lower in ['date', 'datetime', 'timestamp']:
                            exiftool_args.append(f'-DateTimeOriginal={value}')
                        elif key_lower in ['author', 'artist']:
                            exiftool_args.append(f'-Artist={value}')
                        elif key_lower in ['title', 'subject']:
                            exiftool_args.append(f'-Title={value}')
                        else:
                            # Use generic tag for unrecognized metadata
                            # Be careful not to add binary data or huge fields
                            pass 

                if len(exiftool_args) > len(exiftool_base_args):  # If we added any metadata
                    exiftool_args.append(filename_out)
                    try:
                        stdout, stderr = run_exiftool_with_argsfile(exiftool_args)
                        logging.debug(f"exiftool stdout: {stdout}")
                        logging.debug(f"exiftool stderr: {stderr}")
                    except subprocess.CalledProcessError as e:
                        logging.warning(f"Failed to write metadata for {filename}: {safe_decode(e.stderr)}")

        if DELETE_ORIGINALS:
            # Verify the WebP file was created successfully before deleting the original
            if os.path.exists(filename_out) and os.path.getsize(filename_out) > 0:
                try:
                    os.remove(filename)
                    logging.info(f"Deleted original file: {filename}")
                except OSError as e:
                    logging.error(f"Error deleting original file {filename}: {e}")
            else:
                logging.error(f"Failed to create WebP file for {filename} (or file is empty), original not deleted")

        # Cleanup ExifTool backup files if they exist
        backup_file = f"{filename_out}_original"
        if os.path.exists(backup_file):
            try:
                os.remove(backup_file)
                logging.debug(f"Deleted ExifTool backup: {backup_file}")
            except OSError as e:
                logging.warning(f"Could not delete backup file {backup_file}: {e}")

        logging.info(f"Successfully processed: {file_path}")

    except Exception as e:
        logging.error(f"Unexpected error processing {file_path}: {str(e)}")

def validate_folder_path(path: str) -> Optional[str]:
    """Validate the folder path and return normalized path or None if invalid."""
    if not path:
        logging.error("No folder path provided.")
        return None

    # Clean the path
    path = path.strip("\"'")

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

def main() -> None:
    # Load configuration from file
    config = load_config()

    parser = argparse.ArgumentParser(description="Convert images to WebP format")
    parser.add_argument("folder_path", nargs='?', help="Folder path containing images to convert")
    parser.add_argument("--quality", "-q", type=str, default=config.get("webp_quality", DEFAULT_WEBP_QUALITY), help="WebP quality (0-100)")
    parser.add_argument("--keep-originals", action="store_true", help="Keep original files after conversion")
    parser.add_argument("--delete-originals", action="store_false", dest="keep_originals", help="Delete original files after conversion (default behavior)")
    parser.add_argument("--workers", "-w", type=int, default=config.get("max_workers", DEFAULT_MAX_WORKERS), help=f"Number of worker threads (default: from config)")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be converted without making changes")
    parser.add_argument("--formats", nargs='+', default=config.get("supported_formats", DEFAULT_SUPPORTED_FORMATS), help="Supported image formats (default: from config)")
    parser.add_argument("--log-file", type=str, help="Log file path to write logs")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose logging")
    parser.add_argument("--config", type=str, default="config.json", help="Configuration file path (default: config.json)")

    args = parser.parse_args()

    # Setup logging
    setup_logging(log_file=args.log_file, verbose=args.verbose)

    # Update global settings based on arguments and config
    global WEBP_QUALITY, DELETE_ORIGINALS, MAX_WORKERS, SUPPORTED_FORMATS, TOOL_PATHS
    WEBP_QUALITY = args.quality
    DELETE_ORIGINALS = not args.keep_originals
    MAX_WORKERS = args.workers
    SUPPORTED_FORMATS = set(args.formats)
    
    # Load tool paths from config if available
    config_tool_paths = config.get("tool_paths", {})
    if config_tool_paths:
        TOOL_PATHS.update(config_tool_paths)
    
    # Resolve relative tool paths against the script's directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for tool_key, tool_path in TOOL_PATHS.items():
        if not os.path.isabs(tool_path) and os.path.dirname(tool_path):
            resolved = os.path.join(script_dir, tool_path)
            if os.path.exists(resolved):
                TOOL_PATHS[tool_key] = resolved

    # Check if external tools are available
    if not check_external_tools():
        return

    # Log startup info
    logging.info(f"Starting WebP conversion with quality={args.quality}, workers={args.workers}")
    if args.keep_originals:
        logging.info("Original files will be kept after conversion")
    else:
        logging.info("Original files will be deleted after conversion")

    # Get folder path from argument or input
    if args.folder_path:
        folder_path = args.folder_path
    else:
        folder_path = input("Enter the folder path containing images: ")

    # Validate folder path
    validated_path = validate_folder_path(folder_path)
    if not validated_path:
        return

    folder_path = validated_path

    if args.dry_run:
        logging.info("DRY RUN MODE: No files will be modified.")

    try:
        image_files: List[str] = []
        for root, _, files in os.walk(folder_path):
            for file in files:
                if any(file.lower().endswith(ext) for ext in SUPPORTED_FORMATS):
                    file_path = os.path.join(root, file)
                    # Validate individual file
                    if os.path.isfile(file_path) and os.access(file_path, os.R_OK):
                        image_files.append(file_path)
                    else:
                        logging.warning(f"Skipping inaccessible file: {file_path}")
    except PermissionError:
        logging.error(f"Permission denied while accessing folder: {folder_path}")
        return
    except Exception as e:
        logging.error(f"Error while scanning folder: {str(e)}")
        return

    total_files = len(image_files)
    if total_files == 0:
        logging.info("No supported image files found in the specified folder.")
        return

    logging.info(f"Found {total_files} image files to process.")

    if args.dry_run:
        logging.info("Files to be processed:")
        for file_path in image_files:
            logging.info(f"  {file_path}")
        logging.info("Dry run completed. No files were modified.")
        return

    # Use tqdm for progress bar
    with tqdm(total=total_files, desc="Converting images") as pbar:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Submit all tasks
            future_to_file = {executor.submit(process_image_file, file_path): file_path for file_path in image_files}

            # Process completed tasks
            for future in as_completed(future_to_file):
                file_path = future_to_file[future]
                try:
                    future.result()  # This will raise any exceptions that occurred
                except Exception as e:
                    logging.error(f"Error processing file {file_path}: {str(e)}")
                pbar.update(1)  # Update progress bar

    logging.info("All files processed!")

if __name__ == "__main__":
    main()
